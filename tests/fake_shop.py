"""A tiny local shop for crawl tests. One server, several "domains" via loopback
addresses (127.0.0.1, 127.0.0.2, ... all reach a server bound to 0.0.0.0).

    127.0.0.1  polite shop: robots.txt with Crawl-delay and a disallowed path
    127.0.0.2  rate-limits /limited with 429 + Retry-After
    127.0.0.3  shows a CAPTCHA on /captcha
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROBOTS_A = "User-agent: *\nDisallow: /private\nCrawl-delay: 0.2\n"


def product_page(n: int) -> str:
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": f"Shelf {n}",
        "gtin13": f"40000000000{n:02d}",
        "offers": {"@type": "Offer", "price": f"{10 + n}.99", "priceCurrency": "EUR"},
    }
    return (
        f"<html lang='de'><head><title>Shelf {n}</title>"
        f"<script type='application/ld+json'>{json.dumps(ld)}</script></head>"
        f"<body><table class='specs'><tr><th>Breite</th><td>{40 + n} cm</td></tr>"
        f"<tr><th>Gewicht</th><td>{n},5 kg</td></tr></table></body></html>"
    )


class FakeShop:
    def __init__(self):
        self.requests: list[tuple[float, str, str]] = []  # (time, host, path)
        shop = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                host = (self.headers.get("Host") or "").split(":")[0]
                shop.requests.append((time.monotonic(), host, self.path))
                status, body, headers = shop.route(host, self.path)
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def route(self, host: str, path: str) -> tuple[int, str, dict[str, str]]:
        if host == "127.0.0.1":
            if path == "/robots.txt":
                return 200, ROBOTS_A, {"Content-Type": "text/plain"}
            if path.startswith("/p/"):
                return 200, product_page(int(path.rsplit("/", 1)[-1])), {}
            return 200, product_page(99), {}
        if host == "127.0.0.2":
            if path == "/robots.txt":
                return 404, "not found", {}
            if path == "/limited":
                return 429, "slow down", {"Retry-After": "30"}
            return 200, product_page(1), {}
        if host == "127.0.0.3":
            if path == "/robots.txt":
                return 404, "", {}
            if path == "/captcha":
                return (
                    200,
                    "<html><body><div class='g-recaptcha' data-sitekey='x'></div></body></html>",
                    {},
                )
            return 200, product_page(2), {}
        return 404, "unknown host", {}

    def url(self, host: str, path: str) -> str:
        return f"http://{host}:{self.port}{path}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def paths(self, host: str) -> list[str]:
        return [p for _, h, p in self.requests if h == host]

    def times(self, host: str) -> list[float]:
        return [t for t, h, _ in self.requests if h == host]
