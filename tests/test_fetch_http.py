"""The worker's HTTP fetch under hostile responses (found in an independent review)."""

import asyncio
import gzip
import zlib

import pytest

from pos_worker.fetch import http


class Server:
    """A tiny HTTP/1.1 server whose responses are scripted per path."""

    def __init__(self):
        self.routes = {}
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    def url(self, path, host="127.0.0.1"):
        return f"http://{host}:{self.port}{path}"

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            path = line.split()[1].decode()
            await self.routes[path](writer)
        except Exception:  # noqa: BLE001 - the client may hang up on purpose
            pass
        finally:
            writer.close()


def respond(body=b"", status=200, headers=()):
    async def send(w):
        head = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}", *headers, "", ""]
        w.write("\r\n".join(head).encode() + body)
        await w.drain()

    return send


@pytest.fixture
def lab(monkeypatch):
    monkeypatch.setenv("POS_WORKER_ALLOW_PRIVATE_TARGETS", "1")


async def test_decompression_bombs_stop_at_the_limit(lab):
    bomb = gzip.compress(b"\0" * 50_000_000)  # ~50 KB on the wire
    async with Server() as s:
        s.routes["/gz"] = respond(bomb, headers=["Content-Encoding: gzip"])
        s.routes["/stacked"] = respond(bomb, headers=["Content-Encoding: gzip, gzip"])
        s.routes["/zstd"] = respond(b"(\xb5/\xfd", headers=["Content-Encoding: zstd"])
        s.routes["/ok"] = respond(
            zlib.compress(b"hello " * 1000), headers=["Content-Encoding: deflate"]
        )
        async with http.make_client("t") as c:
            r = await http.fetch(c, s.url("/gz"), max_bytes=1_000_000)
            assert r.error == "too_large" and len(r.body) <= 1_000_000
            assert (await http.fetch(c, s.url("/stacked"))).error == "encoding"
            assert (await http.fetch(c, s.url("/zstd"))).error == "encoding"
            assert (await http.fetch(c, s.url("/ok"))).body == b"hello " * 1000
            req = c.build_request("GET", s.url("/ok"))
            assert req.headers["accept-encoding"] == "gzip, deflate"  # no zstd, no br


async def test_internal_addresses_are_refused(monkeypatch):
    monkeypatch.delenv("POS_WORKER_ALLOW_PRIVATE_TARGETS", raising=False)
    async with Server() as s:
        s.routes["/internal"] = respond(b"router admin page")
        async with http.make_client("t") as c:
            r = await http.fetch(c, s.url("/internal"))
            assert r.error == "blocked_address" and r.body == b""
            r = await http.fetch(c, s.url("/internal", host="localhost"))
            assert r.error == "blocked_address"
    for ip in ("10.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "fd00::1", "::1"):
        assert not http.public_address(ip), ip
    assert not http.public_address("::ffff:192.168.1.1")
    assert http.public_address("93.184.216.34") and http.public_address("2a00:1450::1")


async def test_pages_follow_no_redirects_robots_txt_does(lab):
    """A page redirect goes back to the coordinator (robots.txt of the target path, rate
    slots); only robots.txt follows its redirects itself."""
    async with Server() as s:
        s.routes["/a"] = respond(status=301, headers=["Location: /private/b"])
        s.routes["/private/b"] = respond(b"landed")
        s.routes["/ftp"] = respond(status=302, headers=["Location: ftp://x.example/f"])
        s.routes["/loop"] = respond(status=302, headers=["Location: /loop"])
        async with http.make_client("t") as c:
            r = await http.fetch(c, s.url("/a"))
            assert r.status == 301 and r.body == b""
            assert r.redirected_to == s.url("/private/b")  # same host: still not followed
            r = await http.fetch(c, s.url("/a"), follow_redirects=True)
            assert r.body == b"landed" and r.final_url.endswith("/private/b")
            r = await http.fetch(c, s.url("/ftp"), follow_redirects=True)
            assert r.error == "bad_url"  # never a non-http scheme
            r = await http.fetch(c, s.url("/loop"), follow_redirects=True)
            assert r.error == "redirects"


async def test_one_deadline_for_the_whole_fetch(lab):
    async def trickle(w):
        w.write(b"HTTP/1.1 200 X\r\nContent-Length: 100\r\n\r\n")
        for _ in range(100):
            w.write(b"x")
            await w.drain()
            await asyncio.sleep(0.2)

    async with Server() as s:
        s.routes["/slow"] = trickle
        async with http.make_client("t", timeout_s=1.0) as c:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            r = await http.fetch(c, s.url("/slow"), total_timeout_s=1.0)
            assert r.error == "timeout" and loop.time() - t0 < 2.0


async def test_broken_gzip_is_an_encoding_error(lab):
    async with Server() as s:
        s.routes["/bad"] = respond(b"not gzip at all", headers=["Content-Encoding: gzip"])
        async with http.make_client("t") as c:
            assert (await http.fetch(c, s.url("/bad"))).error == "encoding"


async def test_overflow_keeps_the_prefix_that_fits(lab):
    """A gzip robots.txt whose first chunk inflates past the limit must keep its start
    (a leading Disallow: / must not turn into an empty, allow-all file)."""
    body = gzip.compress(b"User-agent: *\nDisallow: /\n" + b"#" * 2_000_000)
    async with Server() as s:
        s.routes["/robots.txt"] = respond(body, headers=["Content-Encoding: gzip"])
        s.routes["/plain"] = respond(b"x" * 5000)
        async with http.make_client("t") as c:
            r = await http.fetch(c, s.url("/robots.txt"), max_bytes=100_000)
            assert r.error == "too_large" and len(r.body) == 100_000
            assert r.body.startswith(b"User-agent: *\nDisallow: /")
            r = await http.fetch(c, s.url("/plain"), max_bytes=1000)
            assert r.error == "too_large" and r.body == b"x" * 1000


async def test_robots_txt_follows_redirects_to_other_hosts(lab):
    """RFC 9309: robots.txt redirects are followed, also to www. or a CDN."""
    async with Server() as s:
        s.routes["/robots.txt"] = respond(
            status=301, headers=[f"Location: http://localhost:{s.port}/real-robots.txt"]
        )
        s.routes["/real-robots.txt"] = respond(b"User-agent: *\nDisallow: /\n")
        async with http.make_client("t") as c:
            pages = await http.fetch(c, s.url("/robots.txt"))
            assert pages.redirected_to and pages.body == b""  # pages: not followed
            r = await http.fetch(c, s.url("/robots.txt"), follow_redirects=True)
            assert r.status == 200 and r.body.startswith(b"User-agent: *")
