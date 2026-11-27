"""robots.txt evaluation (RFC 9309), with our own matcher.

urllib.robotparser differs between Python versions (first match vs. longest match,
no `*` / `$` before 3.13) and matches user-agents by substring; robots.txt decides
what may be fetched, so its semantics are implemented here, version-independent.

Status handling as in RFC 9309: 4xx = no rules (allow all), 5xx / unreachable =
disallow all until it can be fetched again, 200 = the rules.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, unquote, urlsplit

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


def _token(value: str) -> str:
    return value.split("/", 1)[0].strip().lower()


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str  # as written, percent-encoding normalised
    regex: re.Pattern[str]


def _normalise(path: str) -> str:
    """Percent-encoding as in RFC 9309 matching: decode unreserved, upper-case the rest,
    encode non-ASCII as UTF-8 octets."""
    return quote(unquote(path), safe="/?=&;:@!$'()*+,-._~%")


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


@lru_cache(maxsize=4096)
def _rules(text: str, agent: str) -> tuple[Rule, ...]:
    """The rules that apply to `agent` (RFC 9309 2.2): all groups naming its product
    token (case-insensitive, merged), else all `*` groups, else none."""
    ours, groups = _token(agent), {"ours": [], "star": []}
    current: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                current, in_rules = [], False
            current.append(_token(value))
            continue
        if key not in ("allow", "disallow"):
            continue
        in_rules = True
        if not value:
            continue  # "Disallow:" (empty) allows everything: no rule
        path = _normalise(value)
        rule = Rule(key == "allow", path, _compile(path))
        if ours in current:
            groups["ours"].append(rule)
        if "*" in current:
            groups["star"].append(rule)
    return tuple(groups["ours"] or groups["star"])


def allowed(kind: str, text: str | None, url: str, agent: str = PRODUCT_TOKEN) -> bool:
    """RFC 9309 2.2.2: the longest matching path wins, Allow over Disallow on a tie,
    `*` matches any sequence and `$` ends the pattern; /robots.txt is always allowed."""
    if kind == "none":
        return True
    if kind == "unreachable":
        return False
    parts = urlsplit(url)
    target = _normalise((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
    if target == "/robots.txt":
        return True
    best: Rule | None = None
    for rule in _rules(text or "", agent):
        if rule.regex.match(target) and (
            best is None
            or len(rule.pattern) > len(best.pattern)
            or (len(rule.pattern) == len(best.pattern) and rule.allow)
        ):
            best = rule
    return best is None or best.allow


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
