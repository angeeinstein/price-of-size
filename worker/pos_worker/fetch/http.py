"""Bounded HTTP fetches: time, size and redirect limits, no surprises."""

import time
from dataclasses import dataclass, field

import httpx

MAX_BODY_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
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
    "last-modified",
    "etag",
    "retry-after",
    "server",
    "x-robots-tag",
    "cache-control",
    "location",
    "set-cookie-present",
)


@dataclass
class Response:
    url: str
    final_url: str | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0
    error: str | None = None  # timeout | connect | too_large | tls | protocol | redirects
    truncated: bool = False

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
            except (TypeError, ValueError):
                return None

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")


def make_client(user_agent: str, timeout_s: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
        },
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        http2=False,
    )


async def fetch(client: httpx.AsyncClient, url: str, max_bytes: int = MAX_BODY_BYTES) -> Response:
    started = time.monotonic()
    res = Response(url=url)
    try:
        async with client.stream("GET", url) as r:
            res.status = r.status_code
            res.final_url = str(r.url)
            res.headers = {k.lower(): v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}
            if "set-cookie" in r.headers:
                res.headers["set-cookie-present"] = "1"
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    res.truncated = True
                    res.error = "too_large"
                    break
                chunks.append(chunk)
            res.body = b"".join(chunks)
    except httpx.TimeoutException:
        res.error = "timeout"
    except httpx.TooManyRedirects:
        res.error = "redirects"
    except httpx.ConnectError as e:
        res.error = "tls" if "SSL" in str(e) or "certificate" in str(e).lower() else "connect"
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
        res.error = "protocol"
    res.elapsed_ms = int((time.monotonic() - started) * 1000)
    return res


def is_textual(content_type: str | None) -> bool:
    return content_type is None or content_type.lower().startswith(TEXTUAL)
