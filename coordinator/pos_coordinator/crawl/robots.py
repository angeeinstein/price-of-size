"""robots.txt evaluation (RFC 9309 semantics on top of urllib.robotparser).

Status handling as in RFC 9309: 4xx = no rules (allow all), 5xx / unreachable =
disallow all until it can be fetched again, 200 = the rules.
"""

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


@lru_cache(maxsize=4096)
def _parser(text: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def allowed(kind: str, text: str | None, url: str, agent: str = PRODUCT_TOKEN) -> bool:
    if kind == "none":
        return True
    if kind == "unreachable":
        return False
    return _parser(text or "").can_fetch(agent, url)


def crawl_delay(kind: str, text: str | None, agent: str = PRODUCT_TOKEN) -> float | None:
    """Seconds between requests asked for by Crawl-delay or Request-rate.

    Parsed here rather than with urllib.robotparser, which ignores non-integer
    delays ("Crawl-delay: 0.5" is common). The group for our product token wins
    over the `*` group, as for the rules.
    """
    if kind != "rules" or not text:
        return None
    groups = _delay_groups(text)
    values = groups.get(agent.lower()) or groups.get("*") or []
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
            try:
                n, secs = value.split("/", 1)
                secs = secs.strip().rstrip("smhSMH") or "1"
                delay = float(secs) / float(n)
            except (ValueError, ZeroDivisionError):
                continue
        if delay is not None and delay >= 0:
            for a in agents:
                # product-token match: "PriceOfSizeBot" also matches "priceofsizebot/0.1"
                groups.setdefault(a.split("/")[0], []).append(min(delay, 3600.0))
    return groups
