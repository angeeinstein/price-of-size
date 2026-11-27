"""Recognise refusals: CAPTCHAs, bot walls, rate limiting.

The system never tries to get around these. A block marks the domain; the
block log lets the analysis quantify the resulting sample bias.
"""

import re
from dataclasses import dataclass

# Markers of challenge/bot-wall pages (checked on the first part of the body).
BLOCK_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("captcha:recaptcha", re.compile(r"google\.com/recaptcha|g-recaptcha", re.I)),
    ("captcha:hcaptcha", re.compile(r"hcaptcha\.com|h-captcha", re.I)),
    ("captcha:turnstile", re.compile(r"challenges\.cloudflare\.com/turnstile", re.I)),
    (
        "botwall:cloudflare",
        re.compile(
            r"<title>\s*(Just a moment\.\.\.|Attention Required! \| Cloudflare)"
            r"|cf-browser-verification|/cdn-cgi/challenge-platform/",
            re.I,
        ),
    ),
    ("botwall:datadome", re.compile(r"datadome|captcha-delivery\.com", re.I)),
    ("botwall:perimeterx", re.compile(r"perimeterx|px-captcha|_pxCaptcha", re.I)),
    ("botwall:akamai", re.compile(r"Access Denied</h1>.*Reference&#32;&#35;", re.I | re.S)),
    ("botwall:imperva", re.compile(r"Incapsula incident ID|_Incapsula_Resource", re.I)),
    (
        "botwall:generic",
        re.compile(
            r"(are you a (human|robot)|verify you are (a )?human|unusual traffic from your computer"
            r"|bitte bestätigen sie, dass sie kein roboter sind)",
            re.I,
        ),
    ),
]


@dataclass(frozen=True)
class Verdict:
    kind: str  # ok | throttled | blocked | gone | error
    reason: str | None = None


def classify(status: int | None, body_head: str, error: str | None = None) -> Verdict:
    if error is not None:
        return Verdict("throttled" if error in ("timeout", "connect") else "error", error)
    assert status is not None
    if status == 429:
        return Verdict("throttled", "http_429")
    if status == 503:
        # 503 with a challenge page is a block, otherwise a throttle signal
        marker = _marker(body_head)
        return Verdict("blocked", marker) if marker else Verdict("throttled", "http_503")
    marker = _marker(body_head)
    if marker and status in (200, 401, 403):
        return Verdict("blocked", marker)
    if status == 403:
        return Verdict("throttled", "http_403")  # repeated 403s escalate to a block
    if status in (404, 410):
        return Verdict("gone", f"http_{status}")
    if status >= 500:
        return Verdict("error", f"http_{status}")
    return Verdict("ok")


def _marker(body_head: str) -> str | None:
    for name, pattern in BLOCK_MARKERS:
        if pattern.search(body_head):
            return name
    return None
