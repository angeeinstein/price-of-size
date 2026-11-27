"""robots.txt evaluation (RFC 9309 semantics on top of urllib.robotparser).

Status handling as in RFC 9309: 4xx = no rules (allow all), 5xx / unreachable =
disallow all until it can be fetched again, 200 = the rules.
"""

import re
import urllib.robotparser
from functools import lru_cache

PRODUCT_TOKEN = "PriceOfSizeBot"
ROBOTS_MAX_AGE_S = 24 * 3600
ROBOTS_RETRY_S = 3600  # after an unreachable robots.txt


def status_kind(status: int | None) -> str:
    if status is None or status >= 500:
        return "unreachable"
    if 400 <= status < 500:
        return "none"
    if status == 200:
        return "rules"
    return "none"  # e.g. 3xx loops that ended elsewhere: no usable rules


_OTHER = "x-other-crawler"  # stands for every group that is not ours or "*"


def _token(value: str) -> str:
    return value.split("/", 1)[0].strip().lower()


def _our_groups(text: str, agent: str) -> list[str]:
    """robotparser matches user-agent lines by substring ("User-agent: bot" would apply
    to us, "User-agent: PriceOfSizeBot/1.0" would not). RFC 9309 matches the product
    token, case-insensitively: rewrite the lines so robotparser does exactly that."""
    ours = _token(agent)
    out = []
    for raw in text.splitlines():
        m = re.match(r"\s*user-agent\s*:\s*([^#]*)", raw, re.I)
        if m:
            token = _token(m.group(1))
            name = "*" if token == "*" else ours if token == ours else _OTHER
            out.append(f"User-agent: {name}")
        else:
            out.append(raw)
    return out


@lru_cache(maxsize=4096)
def _parser(text: str, agent: str = PRODUCT_TOKEN) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(_our_groups(text, agent))
    return rp


def allowed(kind: str, text: str | None, url: str, agent: str = PRODUCT_TOKEN) -> bool:
    if kind == "none":
        return True
    if kind == "unreachable":
        return False
    return _parser(text or "", agent).can_fetch(_token(agent), url)


def crawl_delay(kind: str, text: str | None, agent: str = PRODUCT_TOKEN) -> float | None:
    """Seconds between requests asked for by Crawl-delay or Request-rate.

    Parsed here rather than with urllib.robotparser, which ignores non-integer
    delays ("Crawl-delay: 0.5" is common). The group for our product token wins
    over the `*` group, as for the rules.
    """
    if kind != "rules" or not text:
        return None
    groups = _delay_groups(text)
    values = groups.get(_token(agent)) or groups.get("*") or []
    return max(values) if values else None


def _delay_groups(text: str) -> dict[str, list[float]]:
    """user-agent (lowercase) -> delays in seconds, from Crawl-delay and Request-rate."""
    groups: dict[str, list[float]] = {}
    agents: list[str] = []
    in_rules = False  # a user-agent line after rules starts a new group
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
            continue
        in_rules = True
        delay = None
        if key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
        elif key == "request-rate" and "/" in value:
            # "1/5" (seconds), "1/10s", "1/1m", "3/1h", optionally a time window after it
            m = re.match(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([smh]?)", value, re.I)
            if not m:
                continue
            n, span, unit = float(m.group(1)), float(m.group(2)), m.group(3).lower()
            seconds = span * {"": 1, "s": 1, "m": 60, "h": 3600}[unit]
            if n <= 0:
                continue
            delay = seconds / n
        if delay is not None and delay >= 0:
            for a in agents:
                # product-token match: "PriceOfSizeBot" also matches "priceofsizebot/0.1"
                groups.setdefault(_token(a), []).append(min(delay, 3600.0))
    return groups


def sitemaps(text: str | None, limit: int = 100) -> list[str]:
    """`Sitemap:` lines (independent of user-agent groups)."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url.startswith(("http://", "https://")) and url not in out:
                out.append(url)
                if len(out) >= limit:
                    break
    return out
