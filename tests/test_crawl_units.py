"""Pure crawl logic: URLs, robots.txt, rate control, snippet capture, block detection."""

import json

import pytest

from pos_coordinator.crawl import ratecontrol, robots, urls
from pos_worker.fetch import blocks, snippets

# --- URLs -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        (
            "HTTPS://Shop.Example.COM:443/p/1?utm_source=x&id=7#reviews",
            "https://shop.example.com/p/1?id=7",
        ),
        ("http://example.com", "http://example.com/"),
        ("http://example.com:8080/a?b=1&gclid=z&c=", "http://example.com:8080/a?b=1&c="),
        ("https://bücher.example/x", "https://xn--bcher-kva.example/x"),
    ],
)
def test_normalize(raw, norm):
    assert urls.normalize(raw) == norm


@pytest.mark.parametrize("bad", ["ftp://x/y", "mailto:a@b", "https:///nohost", "javascript:1"])
def test_normalize_rejects(bad):
    with pytest.raises(urls.BadUrl):
        urls.normalize(bad)


def test_domain_dedup_and_robots_url():
    u = urls.normalize("https://www.ikea.com/at/de/p/x-123/?utm_medium=a")
    assert urls.domain_of(u) == "www.ikea.com"
    assert urls.dedup_key(u) == urls.dedup_key(
        urls.normalize("https://WWW.ikea.com/at/de/p/x-123/")
    )
    assert urls.robots_url(u) == "https://www.ikea.com/robots.txt"


# --- robots.txt -----------------------------------------------------------------------

ROBOTS = """
User-agent: *
Disallow: /checkout
Crawl-delay: 4

User-agent: PriceOfSizeBot
Disallow: /private
Allow: /private/public
Crawl-delay: 2
"""


def test_robots_rules_for_our_token():
    ok = robots.allowed("rules", ROBOTS, "https://x/p/1")
    private = robots.allowed("rules", ROBOTS, "https://x/private/a")
    assert ok and not private
    assert robots.crawl_delay("rules", ROBOTS) == 2.0


def test_robots_status_semantics_rfc9309():
    assert robots.status_kind(404) == "none" and robots.allowed("none", None, "https://x/any")
    assert robots.status_kind(503) == "unreachable"
    assert robots.status_kind(None) == "unreachable"
    assert not robots.allowed("unreachable", None, "https://x/any")
    assert robots.status_kind(200) == "rules"


def test_robots_request_rate():
    txt = "User-agent: *\nRequest-rate: 1/10\n"
    assert robots.crawl_delay("rules", txt) == 10.0


# --- rate control ---------------------------------------------------------------------


def test_clean_run_raises_rate_in_small_steps_up_to_the_ceiling():
    s = ratecontrol.RateState(rate_rps=1.0)
    decisions = [ratecontrol.observe(s, "ok", None, 200, None, top_rps=1.2) for _ in range(60)]
    ups = [d for d in decisions if d.event == "rate_up"]
    assert len(ups) == 2  # 1.0 -> 1.1 -> 1.2 (ceiling), then no more events
    assert s.rate_rps == pytest.approx(1.2)


def test_throttle_halves_and_cooldown_grows_and_honours_retry_after():
    s = ratecontrol.RateState(rate_rps=1.0)
    d1 = ratecontrol.observe(s, "throttled", "http_429", 100, None, 2.0)
    d2 = ratecontrol.observe(s, "throttled", "http_429", 100, None, 2.0)
    assert (d1.rate_rps, d1.cooldown_s) == (0.5, 60.0)
    assert (d2.rate_rps, d2.cooldown_s) == (0.25, 120.0)
    d3 = ratecontrol.observe(s, "throttled", "http_429", 100, 900, 2.0)
    assert d3.cooldown_s == 900  # Retry-After longer than our backoff wins
    for _ in range(30):
        ratecontrol.observe(s, "throttled", "http_503", 100, None, 2.0)
    assert s.rate_rps == ratecontrol.FLOOR_RPS
    assert (
        ratecontrol.observe(s, "throttled", "x", 1, None, 2.0).cooldown_s
        == ratecontrol.MAX_COOLDOWN_S
    )


def test_rising_latency_is_a_throttle_signal():
    s = ratecontrol.RateState(rate_rps=1.0)
    for _ in range(100):
        ratecontrol.observe(s, "ok", None, 300, None, 2.0)
    events = [ratecontrol.observe(s, "ok", None, 9000, None, 2.0).event for _ in range(10)]
    assert "rate_down" in events
    assert s.rate_rps < 1.0


def test_repeated_403_becomes_a_block_and_errors_a_throttle():
    s = ratecontrol.RateState(rate_rps=1.0)
    verdicts = [ratecontrol.observe(s, "throttled", "http_403", 50, None, 2.0) for _ in range(5)]
    assert verdicts[-1].event == "blocked" and verdicts[-1].reason == "repeated_http_403"
    s = ratecontrol.RateState(rate_rps=1.0)
    out = [ratecontrol.observe(s, "error", "http_500", 50, None, 2.0).event for _ in range(3)]
    assert out == [None, None, "rate_down"]


def test_ceiling_respects_crawl_delay_and_override():
    assert ratecontrol.ceiling(2.0, None, None) == 2.0
    assert ratecontrol.ceiling(2.0, 5.0, None) == 0.2
    assert ratecontrol.ceiling(2.0, 5.0, 0.1) == 0.1


# --- snippets ---------------------------------------------------------------------------

PAGE = """<!doctype html><html lang="de"><head>
<title>KALLAX Regal, weiß - IKEA</title>
<link rel="canonical" href="https://www.ikea.com/at/de/p/kallax-regal-weiss-80275887/">
<meta property="og:price:amount" content="44.99"><meta name="viewport" content="x">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
 "name":"KALLAX","offers":{"@type":"Offer","price":"44.99","priceCurrency":"EUR"}}</script>
<script>var tracking = 1;</script>
</head><body>
<nav aria-label="Breadcrumb" class="breadcrumb"><ol><li>Möbel</li><li>Regale</li></ol></nav>
<div class="product-price"><span class="price__integer">44</span>,99 €</div>
<div class="product-details"><dl><dt>Breite</dt><dd>77 cm</dd>
<dt>Höhe</dt><dd>147 cm</dd></dl></div>
<table class="specs"><tr><th>Gewicht</th><td>16,5 kg</td></tr></table>
<img src="/img/kallax.jpg">
</body></html>"""


def test_capture_keeps_raw_evidence_verbatim():
    s = snippets.capture(PAGE)
    assert s["title"] == "KALLAX Regal, weiß - IKEA"
    assert s["lang"] == "de"
    assert s["canonical"].endswith("80275887/")
    assert s["meta"] == {"og:price:amount": ["44.99"]}
    assert json.loads(s["json_ld"][0])["offers"]["price"] == "44.99"
    assert s["has_structured_data"] is True
    assert any("Regale" in b for b in s["breadcrumbs"])
    assert any("16,5 kg" in t for t in s["tables"])
    assert any("147 cm" in d for d in s["definition_lists"])
    assert any("44" in p for p in s["price_blocks"])
    assert "/img/kallax.jpg" in s["image_urls"]
    assert "tracking" not in json.dumps(s)  # scripts other than JSON-LD are not kept


def test_capture_is_bounded_and_survives_garbage():
    huge = "<html><body>" + "<table><tr><td>x</td></tr></table>" * 5000 + "</body></html>"
    s = snippets.capture(huge)
    assert len(s["tables"]) == snippets.MAX_ITEMS and "tables" in s["truncated"]
    assert snippets.capture("")["parse_error"] is True
    assert snippets.capture(b"\x00\xff not html")["has_structured_data"] is False


# --- blocks --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "kind", "reason"),
    [
        (200, "<html><body>fine</body></html>", "ok", None),
        (429, "", "throttled", "http_429"),
        (503, "maintenance", "throttled", "http_503"),
        (503, "<title>Just a moment...</title>", "blocked", "botwall:cloudflare"),
        (200, '<div class="g-recaptcha" data-sitekey="x">', "blocked", "captcha:recaptcha"),
        (403, "Forbidden", "throttled", "http_403"),
        (404, "nope", "gone", "http_404"),
        (500, "oops", "error", "http_500"),
    ],
)
def test_classify(status, body, kind, reason):
    v = blocks.classify(status, body)
    assert (v.kind, v.reason) == (kind, reason)


def test_classify_network_errors():
    assert blocks.classify(None, "", "timeout").kind == "throttled"
    assert blocks.classify(None, "", "tls").kind == "error"


def test_fractional_crawl_delay_and_group_precedence():
    """robotparser drops non-integer Crawl-delay values; ours must not."""
    assert robots.crawl_delay("rules", "User-agent: *\nCrawl-delay: 0.5\n") == 0.5
    txt = "User-agent: other\nCrawl-delay: 30\n\nUser-agent: *\nCrawl-delay: 1.5\nDisallow: /x\n"
    assert robots.crawl_delay("rules", txt) == 1.5
    txt = "User-agent: *\nCrawl-delay: 9\n\nUser-agent: priceofsizebot\nCrawl-delay: 0.25\n"
    assert robots.crawl_delay("rules", txt) == 0.25
    assert robots.crawl_delay("rules", "User-agent: *\nCrawl-delay: soon\n") is None


def test_breaker_pauses_a_domain_after_a_refusal():
    from pos_worker.fetch import breaker

    breaker.reset()
    breaker.trip("a.example", "ok", None)
    assert not breaker.paused("a.example")
    breaker.trip("a.example", "throttled", 120)
    breaker.trip("b.example", "blocked", None)
    assert breaker.paused("a.example") and breaker.paused("b.example")
    assert not breaker.paused("c.example")
    breaker.reset()


async def test_fetch_limiter_caps_concurrency_and_resizes():
    import asyncio

    from pos_worker.fetch.handlers import _Limiter

    lim = _Limiter(2)
    peak = 0

    async def task():
        nonlocal peak
        async with lim:
            peak = max(peak, lim.active)
            await asyncio.sleep(0.01)

    await asyncio.gather(*(task() for _ in range(10)))
    assert peak == 2
    lim.resize(5)
    peak = 0
    await asyncio.gather(*(task() for _ in range(10)))
    assert peak == 5
