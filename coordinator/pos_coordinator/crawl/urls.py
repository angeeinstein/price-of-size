"""URL normalisation, so the same page is queued once.

Conservative on purpose: only changes that cannot change which page is served
(case of scheme/host, default ports, fragments, well-known tracking parameters).
"""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "igshid",
    "spm",
    "ref_src",
    "srsltid",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_")


class BadUrl(ValueError):
    pass


def normalize(url: str) -> str:
    """Raises BadUrl, and only BadUrl, for anything that is not a usable http(s) URL
    (URLs come from shops' sitemaps and redirects: untrusted)."""
    try:
        return _normalize(url)
    except BadUrl:
        raise
    except (ValueError, TypeError, AttributeError) as e:  # urlsplit: bad port, brackets
        raise BadUrl(f"malformed URL {str(url)[:200]!r}: {e}") from e


def _normalize(url: str) -> str:
    if len(url) > 8000:
        raise BadUrl("URL too long")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise BadUrl(f"unsupported scheme in {url!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise BadUrl(f"no host in {url!r}")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise BadUrl(f"bad host in {url!r}") from e
    port = parts.port
    netloc = (
        host
        if port is None or (scheme, port) in (("http", 80), ("https", 443))
        else f"{host}:{port}"
    )
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
        ],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def domain_of(url: str) -> str:
    """The rate-limit unit: the host name (www.example.com and example.com are separate
    servers as far as politeness goes; they often share limits, so stay conservative
    by keeping per-host rates low)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_of(url: str) -> str | None:
    """The host of an untrusted URL, or None if it has none or is malformed."""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".") or None
    except ValueError:
        return None


def dedup_key(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode()).hexdigest()[:32]


def robots_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))
