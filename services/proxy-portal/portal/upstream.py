"""HTTP to the upstreams (the Determined master, Prometheus): JSON in, JSON out, standard library only."""

import functools
import http.client
import io
import ipaddress
import json
import time
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect: a 3xx raises HTTPError, so a token is never sent to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


class _DeadlineReader(io.RawIOBase):
    """The socket's reads, each with the time left to the deadline as its timeout, and none after it."""

    def __init__(self, sock, deadline):
        super().__init__()
        self._sock = sock
        self._raw = sock.makefile("rb", buffering=0)
        self._deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the request's time limit passed")
        self._sock.settimeout(remaining)
        return self._raw.readinto(buffer)

    def close(self):
        if not self.closed:
            self._raw.close()
        super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    """Reads the status line, the headers and the body through a _DeadlineReader."""

    def __init__(self, sock, *args, deadline, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp.close()
        self.fp = io.BufferedReader(_DeadlineReader(sock, deadline))


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.response_class = functools.partial(_DeadlineResponse, deadline=deadline)


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.response_class = functools.partial(_DeadlineResponse, deadline=deadline)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(functools.partial(_HTTPConnection, deadline=req.deadline), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(functools.partial(_HTTPSConnection, deadline=req.deadline), req, context=self._context)


# Never use the environment's proxy variables: the upstreams are on the private network.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, _HTTPHandler, _HTTPSHandler)


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
    """
    GET (data None) or POST data as JSON; returns the decoded body. Raises HTTPStatusError on 4xx/5xx.

    timeout (default UPSTREAM_TIMEOUT) is in seconds for the whole request, from the connection to the last byte
    of the answer, so an upstream that writes slowly cannot hold it longer.
    """
    _check_target(url)
    timeout = timeout or settings.UPSTREAM_TIMEOUT
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="GET" if data is None else "POST")
    request.deadline = time.monotonic() + timeout
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read(64 * 1024).decode("utf-8"))
        except (OSError, ValueError, http.client.HTTPException):
            parsed = None
        raise HTTPStatusError(e.code, parsed) from None
    except (OSError, ValueError, http.client.HTTPException) as e:
        # http.client.HTTPException: a cut-off body (IncompleteRead), a bad status line, too long a header line
        raise UpstreamError(type(e).__name__) from None
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise UpstreamError("not JSON") from None
