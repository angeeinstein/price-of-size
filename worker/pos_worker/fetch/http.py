"""Bounded HTTP fetches: time, size, redirect and address limits, no surprises.

What a shop (or a sitemap, or a redirect) can make a worker do is limited here:

- **Size:** the body is decompressed by us, chunk by chunk, against the same byte
  limit as the raw body; only gzip and deflate are offered, stacked or unknown
  encodings are refused (a 100 KB response must not inflate to gigabytes).
- **Time:** one deadline for the whole fetch, not per read (a server trickling a
  byte every 29 s must not hold a slot forever).
- **Addresses:** connections go only to public addresses, checked after DNS
  resolution at connect time (no loopback, private, link-local, CGNAT, ULA or
  metadata addresses, also not via DNS rebinding). The worker usually sits in
  someone's home or office network; a crafted sitemap or redirect must not reach
  into it. `POS_WORKER_ALLOW_PRIVATE_TARGETS=1` lifts this (tests, lab shops).
- **Redirects:** pages and sitemaps follow none. A redirect ends the fetch with
  the 3xx and its Location, and the coordinator queues the target like any URL:
  through robots.txt (a redirect to a disallowed path must not slip through) and
  the rate schedule. Only robots.txt itself follows its redirects (RFC 9309),
  across hosts, every hop through the address check.
"""

import asyncio
import ipaddress
import os
import socket
import time
import zlib
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import anyio
import httpcore
import httpx

MAX_BODY_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_TIMEOUT_S = 90.0
TEXTUAL = (
    "text/",
    "application/xhtml",
    "application/xml",
    "application/json",
    "application/ld+json",
)
KEEP_HEADERS = (
    "content-type",
    "content-language",
    "content-encoding",
    "last-modified",
    "etag",
    "retry-after",
    "server",
    "x-robots-tag",
    "cache-control",
    "location",
    "set-cookie-present",
)
REDIRECTS = (301, 302, 303, 307, 308)


def _allow_private() -> bool:
    return os.environ.get("POS_WORKER_ALLOW_PRIVATE_TARGETS", "") in ("1", "true", "yes")


class BlockedAddress(httpcore.ConnectError):
    pass


def public_address(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


class SafeBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself and connects only to a checked address."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if _allow_private():
            return await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        with anyio.fail_after(timeout):
            infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [info[4][0] for info in infos]
        if not addresses or not all(public_address(a) for a in addresses):
            raise BlockedAddress(f"refusing non-public address for {host}: {addresses}")
        last: Exception | None = None
        for address in dict.fromkeys(addresses):  # connect to what was checked, in order
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        raise httpcore.ConnectError(str(last))

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedAddress("unix sockets are not fetch targets")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SafeTransport(httpx.AsyncHTTPTransport):
    def __init__(self, limits: httpx.Limits | None = None) -> None:
        limits = limits or httpx.Limits()
        super().__init__(limits=limits, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._pool._ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=SafeBackend(),
        )


@dataclass
class Response:
    url: str
    final_url: str | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0
    # timeout | connect | blocked_address | too_large | tls | protocol | redirects |
    # encoding | bad_url
    error: str | None = None
    truncated: bool = False
    redirected_to: str | None = None  # a redirect to another host, not followed

    @property
    def retry_after_s(self) -> float | None:
        value = self.headers.get("retry-after")
        if not value:
            return None
        try:
            return max(float(value), 0.0)
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                when = parsedate_to_datetime(value)
                return max(when.timestamp() - time.time(), 0.0)
            except (TypeError, ValueError, OverflowError):
                return None

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")


def make_client(user_agent: str, timeout_s: float = 30.0) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=16)
    return httpx.AsyncClient(
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
            # Only what we decode ourselves, under the size limit (see _Decoder).
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        follow_redirects=False,
        transport=SafeTransport(limits),
        trust_env=False,
    )


class TooLarge(Exception):
    """Over the limit; `prefix` holds the decoded bytes that still fit."""

    def __init__(self, prefix: bytes = b""):
        super().__init__("too large")
        self.prefix = prefix


class BadEncoding(Exception):
    pass


class _Decoder:
    """Content-Encoding gzip / deflate / identity, inflated to at most `limit` bytes."""

    def __init__(self, encoding: str | None, limit: int):
        enc = (encoding or "identity").strip().lower()
        if "," in enc:
            raise BadEncoding(f"stacked content encodings: {enc}")
        self._deflate = enc == "deflate"
        if enc in ("gzip", "x-gzip"):
            self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc in ("deflate", "identity", ""):
            self._z = None  # deflate: zlib-wrapped or raw, decided on the first bytes
        else:
            raise BadEncoding(f"unsupported content encoding: {enc}")
        self.limit = limit
        self.size = 0

    def _take(self, data: bytes) -> bytes:
        """Count `data`; past the limit, keep what fits and stop."""
        room = self.limit - self.size
        if len(data) > room:
            self.size = self.limit
            raise TooLarge(data[: max(room, 0)])
        self.size += len(data)
        return data

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        if self._z is None and self._deflate:
            wbits = zlib.MAX_WBITS if data[:1] == b"\x78" else -zlib.MAX_WBITS
            self._z = zlib.decompressobj(wbits)
        if self._z is None:
            return self._take(data)
        out = []
        try:
            while data and not self._z.eof:
                chunk = self._z.decompress(data, self.limit - self.size + 1)
                try:
                    out.append(self._take(chunk))
                except TooLarge as e:
                    raise TooLarge(b"".join(out) + e.prefix) from None
                data = self._z.unconsumed_tail
        except zlib.error as e:
            raise BadEncoding(str(e)) from e
        return b"".join(out)


async def _get(
    client: httpx.AsyncClient, url: str, res: Response, max_bytes: int, follow: bool
) -> None:
    for _ in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme not in ("http", "https"):
            res.error = "bad_url"
            return
        async with client.stream("GET", url) as r:
            res.status = r.status_code
            res.final_url = str(r.url)
            res.headers = {k.lower(): v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}
            if "set-cookie" in r.headers:
                res.headers["set-cookie-present"] = "1"
            location = r.headers.get("location")
            if r.status_code in REDIRECTS and location:
                target = urljoin(str(r.url), location)
                if not follow:
                    res.redirected_to = target  # the coordinator decides about it
                    return
                url = target
                continue
            decoder = _Decoder(r.headers.get("content-encoding"), max_bytes)
            chunks = []
            try:
                async for raw in r.aiter_raw():
                    chunks.append(decoder.feed(raw))
            except TooLarge as e:
                chunks.append(e.prefix)  # what fits: a truncated robots.txt is still read
                res.truncated = True
                res.error = "too_large"
            res.body = b"".join(chunks)
            return
    res.error = "redirects"


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int = MAX_BODY_BYTES,
    total_timeout_s: float = TOTAL_TIMEOUT_S,
    follow_redirects: bool = False,
) -> Response:
    """`follow_redirects`: robots.txt only (RFC 9309 follows its redirects, also to
    www. or a CDN); every hop still passes the address check."""
    started = time.monotonic()
    res = Response(url=url)
    try:
        async with asyncio.timeout(total_timeout_s):
            await _get(client, url, res, max_bytes, follow_redirects)
    except (TimeoutError, httpx.TimeoutException):
        res.error = "timeout"
    except BadEncoding:
        res.error = "encoding"
    except httpx.ConnectError as e:
        if "refusing non-public" in str(e) or "unix sockets" in str(e):
            res.error = "blocked_address"
        elif "SSL" in str(e) or "certificate" in str(e).lower():
            res.error = "tls"
        else:
            res.error = "connect"
    except (httpx.InvalidURL, httpx.UnsupportedProtocol, ValueError):
        res.error = "bad_url"
    except httpx.HTTPError:  # protocol, read/write, decoding, proxy, ...
        res.error = "protocol"
    res.elapsed_ms = int((time.monotonic() - started) * 1000)
    return res


def is_textual(content_type: str | None) -> bool:
    return content_type is None or content_type.lower().startswith(TEXTUAL)
