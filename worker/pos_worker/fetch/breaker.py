"""Local circuit breaker per domain.

The coordinator decides rates and blocks, but its answer to a 429 or a CAPTCHA
arrives only with the next result batch. Meanwhile this worker may hold more
jobs for the same domain (their slots were handed out in advance). Once a
domain answered with a refusal, those jobs are skipped without sending a
request and go back to the coordinator, which reschedules them.
"""

import time

BLOCK_PAUSE_S = 3600.0  # if the job does not say (the coordinator's default first pause)
MAX_BLOCK_PAUSE_S = 7 * 86400.0
MIN_THROTTLE_PAUSE_S = 60.0

_paused_until: dict[str, float] = {}
_tripped_at: dict[str, float] = {}  # wall clock of the latest trip, per domain


def block_pause(payload: dict) -> float:
    """The pause after a challenge page, as the coordinator configured it (the job's
    `refusal_pause_s`, from the block policy); untrusted, so bounded."""
    value = payload.get("refusal_pause_s")
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return min(float(value), MAX_BLOCK_PAUSE_S)
    return BLOCK_PAUSE_S


def trip(
    domain: str, verdict: str, retry_after_s: float | None, block_pause_s: float = BLOCK_PAUSE_S
) -> None:
    if verdict == "blocked":
        pause = block_pause_s
    elif verdict == "throttled":
        pause = max(retry_after_s or 0.0, MIN_THROTTLE_PAUSE_S)
    else:
        return
    if pause <= 0:
        return
    until = time.monotonic() + pause
    _paused_until[domain] = max(_paused_until.get(domain, 0.0), until)
    _tripped_at[domain] = time.time()


def release_if_unblocked(domain: str, payload: dict) -> None:
    """The coordinator unblocked the domain (by hand or as an automatic retry) after
    this worker paused it: the job says when (`unblocked_at`, epoch seconds), and a
    pause from before that no longer holds."""
    when = payload.get("unblocked_at")
    if not isinstance(when, int | float) or isinstance(when, bool):
        return
    tripped = _tripped_at.get(domain)
    if tripped is not None and when > tripped:
        _paused_until.pop(domain, None)
        _tripped_at.pop(domain, None)


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
    _tripped_at.clear()
