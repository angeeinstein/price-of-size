"""Local circuit breaker per domain.

The coordinator decides rates and blocks, but its answer to a 429 or a CAPTCHA
arrives only with the next result batch. Meanwhile this worker may hold more
jobs for the same domain (their slots were handed out in advance). Once a
domain answered with a refusal, those jobs are skipped without sending a
request and go back to the coordinator, which reschedules them.
"""

import time

BLOCK_PAUSE_S = 6 * 3600.0
MIN_THROTTLE_PAUSE_S = 60.0

_paused_until: dict[str, float] = {}


def trip(domain: str, verdict: str, retry_after_s: float | None) -> None:
    if verdict == "blocked":
        pause = BLOCK_PAUSE_S
    elif verdict == "throttled":
        pause = max(retry_after_s or 0.0, MIN_THROTTLE_PAUSE_S)
    else:
        return
    until = time.monotonic() + pause
    _paused_until[domain] = max(_paused_until.get(domain, 0.0), until)


def paused(domain: str) -> bool:
    until = _paused_until.get(domain)
    if until is None:
        return False
    if time.monotonic() >= until:
        del _paused_until[domain]
        return False
    return True


def reset() -> None:
    _paused_until.clear()
