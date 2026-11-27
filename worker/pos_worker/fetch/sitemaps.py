"""Sitemap parsing (sitemaps.org protocol): urlset, sitemap index, plain text, gzip.

Defensive: no entity resolution, no network access from the parser, and the
protocol's limits (50,000 URLs, 50 MB uncompressed) enforced, so a hostile or
broken sitemap cannot exhaust a node (e.g. a gzip bomb).
"""

import gzip
import io
import zlib
from dataclasses import dataclass, field

from lxml import etree

MAX_URLS = 50_000
MAX_BYTES = 50 * 1024 * 1024


class SitemapError(ValueError):
    pass


@dataclass
class Sitemap:
    kind: str  # urlset | index | text
    urls: list[dict[str, str | None]] = field(default_factory=list)  # loc, lastmod
    sitemaps: list[str] = field(default_factory=list)  # children of an index
    truncated: bool = False


def decompress(data: bytes, limit: int = MAX_BYTES) -> bytes:
    if data[:2] != b"\x1f\x8b":
        return data[:limit]
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, limit + 1)
    if len(out) > limit:
        raise SitemapError("sitemap larger than 50 MB uncompressed")
    return out


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse(data: bytes) -> Sitemap:
    body = decompress(data)
    head = body[:512].lstrip()
    if not head.startswith(b"<"):
        return _parse_text(body)
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, recover=True, remove_comments=True
    )
    try:
        root = etree.parse(io.BytesIO(body), parser).getroot()
    except etree.XMLSyntaxError as e:
        raise SitemapError(f"not a sitemap: {e}") from e
    if root is None:
        raise SitemapError("empty document")
    kind = _local(root.tag)
    out = Sitemap(kind="index" if kind == "sitemapindex" else "urlset")
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"unexpected root element {kind!r}")
    for entry in root:
        name = _local(entry.tag)
        loc = lastmod = None
        for child in entry:
            n = _local(child.tag)
            if n == "loc" and child.text:
                loc = child.text.strip()
            elif n == "lastmod" and child.text:
                lastmod = child.text.strip()[:40]
        if not loc:
            continue
        if kind == "sitemapindex" and name == "sitemap":
            out.sitemaps.append(loc)
        elif kind == "urlset" and name == "url":
            out.urls.append({"loc": loc, "lastmod": lastmod})
        if len(out.urls) + len(out.sitemaps) >= MAX_URLS:
            out.truncated = True
            break
    return out


def _parse_text(body: bytes) -> Sitemap:
    out = Sitemap(kind="text")
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(("http://", "https://")):
            out.urls.append({"loc": line, "lastmod": None})
            if len(out.urls) >= MAX_URLS:
                out.truncated = True
                break
    return out


def gzip_bytes(data: bytes) -> bytes:  # for tests
    return gzip.compress(data)
