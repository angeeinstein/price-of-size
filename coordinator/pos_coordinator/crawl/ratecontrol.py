"""Per-(domain, egress group) adaptive rate, like TCP congestion control (AIMD).

* After a run of clean responses the rate rises in small steps (+10 %).
* On 429/503, timeouts, rising response times or a block page it halves and
  pauses (cooldown), honouring Retry-After. Repeated throttling lengthens the
  cooldown exponentially, from a minute up to six hours.
* The rate stays between a floor and the ceiling (default 2 requests/s), and
  never above robots.txt's crawl-delay or a per-domain override.

Pure functions over `RateState`, so the policy is unit-tested in isolation.
"""

from dataclasses import dataclass

INCREASE_FACTOR = 1.10
DECREASE_FACTOR = 0.5
CLEAN_RUN = 20  # clean responses before a rate increase
FLOOR_RPS = 1 / 120  # never slower than one request per two minutes (while not cooling down)
BASE_COOLDOWN_S = 60.0
MAX_COOLDOWN_S = 6 * 3600.0
LATENCY_ALPHA = 0.2  # EWMA weight of the newest sample
BASELINE_ALPHA = 0.02  # slow EWMA: the domain's normal latency
LATENCY_FACTOR = 3.0  # "rising response times": fast EWMA above 3x baseline...
LATENCY_MIN_MS = 2000.0  # ...and above 2 s
ERROR_RUN = 3  # consecutive 5xx/other errors count as a throttle signal
FORBIDDEN_RUN = 5  # consecutive 403s turn into a block


@dataclass
class RateState:
    rate_rps: float
    clean_streak: int = 0
    throttle_count: int = 0
    latency_ewma_ms: float | None = None
    latency_base_ms: float | None = None
    consecutive_errors: int = 0
    consecutive_403: int = 0


@dataclass(frozen=True)
class Decision:
    rate_rps: float
    cooldown_s: float = 0.0
    event: str | None = None  # rate_up | rate_down | blocked | None
    reason: str | None = None


def ceiling(max_rps: float, crawl_delay_s: float | None, override_rps: float | None) -> float:
    top = max_rps
    if crawl_delay_s:
        top = min(top, 1.0 / crawl_delay_s)
    if override_rps:
        top = min(top, override_rps)
    return top


def _throttle(s: RateState, reason: str, retry_after_s: float | None) -> Decision:
    s.rate_rps = max(s.rate_rps * DECREASE_FACTOR, FLOOR_RPS)
    s.throttle_count += 1
    s.clean_streak = 0
    cooldown = min(BASE_COOLDOWN_S * 2 ** (s.throttle_count - 1), MAX_COOLDOWN_S)
    if retry_after_s:
        cooldown = min(max(cooldown, retry_after_s), MAX_COOLDOWN_S)
    return Decision(s.rate_rps, cooldown, "rate_down", reason)


def observe(
    s: RateState,
    verdict: str,
    reason: str | None,
    elapsed_ms: int | None,
    retry_after_s: float | None,
    top_rps: float,
) -> Decision:
    """Update the state with one fetch outcome and say what to do."""
    s.rate_rps = min(s.rate_rps, top_rps)
    if verdict == "blocked":
        s.clean_streak = 0
        return Decision(s.rate_rps, MAX_COOLDOWN_S, "blocked", reason)

    if reason == "http_403":
        s.consecutive_403 += 1
        if s.consecutive_403 >= FORBIDDEN_RUN:
            return Decision(s.rate_rps, MAX_COOLDOWN_S, "blocked", "repeated_http_403")
    else:
        s.consecutive_403 = 0

    if verdict == "throttled":
        s.consecutive_errors = 0
        return _throttle(s, reason or "throttled", retry_after_s)

    if verdict == "error":
        s.consecutive_errors += 1
        if s.consecutive_errors >= ERROR_RUN:
            s.consecutive_errors = 0
            return _throttle(s, f"repeated_{reason}", retry_after_s)
        return Decision(s.rate_rps)
    s.consecutive_errors = 0

    # ok or gone: a clean response
    if elapsed_ms is not None:
        s.latency_ewma_ms = (
            elapsed_ms
            if s.latency_ewma_ms is None
            else LATENCY_ALPHA * elapsed_ms + (1 - LATENCY_ALPHA) * s.latency_ewma_ms
        )
        s.latency_base_ms = (
            elapsed_ms
            if s.latency_base_ms is None
            else BASELINE_ALPHA * elapsed_ms + (1 - BASELINE_ALPHA) * s.latency_base_ms
        )
        if (
            s.latency_ewma_ms > LATENCY_FACTOR * s.latency_base_ms
            and s.latency_ewma_ms > LATENCY_MIN_MS
        ):
            s.latency_ewma_ms = s.latency_base_ms  # one signal per episode
            return _throttle(s, "rising_latency", None)

    s.clean_streak += 1
    if s.clean_streak >= CLEAN_RUN:
        s.clean_streak = 0
        if s.throttle_count:
            s.throttle_count -= 1  # calm periods earn back shorter cooldowns
        new = min(s.rate_rps * INCREASE_FACTOR, top_rps)
        if new > s.rate_rps + 1e-12:
            s.rate_rps = new
            return Decision(new, 0.0, "rate_up", "clean_run")
    return Decision(s.rate_rps)
