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
                gz = shop.gz_route(host, self.path)
                if gz is not None:
                    status, data, headers = 200, gz, {"Content-Type": "application/x-gzip"}
                else:
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
        if host == "127.0.0.4":  # discovery through sitemaps
            base = f"http://127.0.0.4:{self.port}"
            if path == "/robots.txt":
                return 200, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap_index.xml\n", {}
            if path == "/sitemap_index.xml":
                return (
                    200,
                    (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<sitemap><loc>{base}/sitemap-products.xml.gz</loc></sitemap>"
                        f"<sitemap><loc>{base}/sitemap-pages.xml</loc></sitemap>"
                        "<sitemap><loc>http://127.0.0.9/foreign.xml</loc></sitemap>"
                        "</sitemapindex>"
                    ),
                    {"Content-Type": "application/xml"},
                )
            if path == "/sitemap-pages.xml":
                return (
                    200,
                    (
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{base}/about</loc></url></urlset>"
                    ),
                    {"Content-Type": "application/xml"},
                )
            if path.startswith("/p/"):
                return 200, product_page(int(path.rsplit("/", 1)[-1])), {}
            return 200, "<html><body>about us</body></html>", {}
        return 404, "unknown host", {}

    def gz_route(self, host: str, path: str) -> bytes | None:
        if host == "127.0.0.4" and path == "/sitemap-products.xml.gz":
            import gzip

            base = f"http://127.0.0.4:{self.port}"
            urls = "".join(
                f"<url><loc>{base}/p/{i}</loc><lastmod>2026-11-0{i}</lastmod></url>"
                for i in range(1, 4)
            )
            urls += "<url><loc>http://127.0.0.9/p/77</loc></url>"  # another host: ignored
            return gzip.compress(
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()
            )
        return None

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
