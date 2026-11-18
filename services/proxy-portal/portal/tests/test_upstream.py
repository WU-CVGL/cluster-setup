"""The time limit of an upstream request covers the whole answer, against a real local server that writes slowly."""

import json
import time

from django.test import SimpleTestCase

from portal import upstream

from . import stubs


def http_answer(data, status="200 OK"):
    """A complete HTTP answer with a JSON body: (status line and headers, body)."""
    body = json.dumps(data).encode()
    head = (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n").encode()
    return head, body


class SlowServer:
    def __init__(self):
        self.answer = None

    def handle(self, method, path, headers, body):
        return self.answer


class DeadlineTest(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stub = SlowServer()
        cls.server, cls.url = stubs.serve(cls.stub)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def timed(self, timeout):
        start = time.monotonic()
        try:
            return upstream.request_json(self.url + "/x", timeout=timeout), time.monotonic() - start
        except upstream.UpstreamError as e:
            return e, time.monotonic() - start

    def test_a_fast_answer(self):
        head, body = http_answer({"ok": True})
        self.stub.answer = ("slow", (head + body, b"", 0))
        self.assertEqual(self.timed(1)[0], {"ok": True})

    def test_a_body_written_slowly(self):
        head, body = http_answer({"users": ["a" * 20]})  # about 3.5 s at 0.1 s a byte
        self.stub.answer = ("slow", (head, body, 0.1))
        answer, seconds = self.timed(1)
        self.assertIsInstance(answer, upstream.UpstreamError)
        self.assertLess(seconds, 1.5)

    def test_headers_written_slowly(self):
        head, body = http_answer({"ok": True})
        self.stub.answer = ("slow", (b"", head + body, 0.05))  # about 5 s
        answer, seconds = self.timed(1)
        self.assertIsInstance(answer, upstream.UpstreamError)
        self.assertLess(seconds, 1.5)

    def test_a_body_that_stops(self):
        head, body = http_answer({"users": ["a" * 20]})
        self.stub.answer = ("slow", (head + body[:5], body[5:], 3))  # part at once, then nothing for 3 s
        answer, seconds = self.timed(1)
        self.assertIsInstance(answer, upstream.UpstreamError)
        self.assertLess(seconds, 1.5)

    def test_an_error_body_written_slowly(self):
        head, body = http_answer({"code": 13, "message": "x" * 40}, status="500 Internal Server Error")
        self.stub.answer = ("slow", (head, body, 0.1))
        answer, seconds = self.timed(1)
        self.assertIsInstance(answer, upstream.HTTPStatusError)  # the status, without the body that never came
        self.assertEqual((answer.status, answer.body), (500, None))
        self.assertLess(seconds, 1.5)
