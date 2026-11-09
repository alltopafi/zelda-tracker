"""Offline tests. The HTML here is synthetic: it exercises the parsing and
decision logic, not the retailers' real markup."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from tracker.config import ConfigError, load_config, parse_config
from tracker.models import Element, Page, Status, Verdict
from tracker.notify import Notifier, allowed_mentions
from tracker.parse import classify_availability, extract, jsonld_offers, to_price
from tracker.retailers import REGISTRY
from tracker.scheduler import Scheduler, apply_price_cap
from tracker.state import Entry, State, apply_verdict, load_state, save_state

ROOT = Path(__file__).resolve().parent.parent


def ld(availability: str, price="99.99", extra: dict | None = None) -> str:
    doc = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Thing",
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "USD", "availability": availability},
    }
    doc.update(extra or {})
    return f'<html><head><title>Thing</title><script type="application/ld+json">{json.dumps(doc)}</script></head><body></body></html>'


def page(html="", status=200, **kw) -> Page:
    return Page(url="https://example.test/p", status=status, html=html, **kw)


# ---- parsing ----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://schema.org/InStock", True), ("http://schema.org/OutOfStock", False),
    ("InStock", True), ("in stock", True), ("PreOrder", True), ("SoldOut", False),
    ("https://schema.org/BackOrder", True), ("Discontinued", False), ("oos", False),
    ("weird", None), (None, None), (5, None),
])
def test_classify_availability(raw, expected):
    assert classify_availability(raw) is expected


@pytest.mark.parametrize("raw,expected", [
    ("49.99", 49.99), (49.99, 49.99), ("$1,299.00", 1299.0), ("", None), (None, None), (0, None), (True, None),
])
def test_to_price(raw, expected):
    assert to_price(raw) == expected


def test_jsonld_graph_and_offer_list():
    doc = {"@graph": [
        {"@type": "BreadcrumbList"},
        {"@type": ["Product"], "offers": [
            {"availability": "https://schema.org/OutOfStock", "price": 10},
            {"availability": "https://schema.org/InStock", "priceSpecification": {"price": "12.50"}},
        ]},
    ]}
    assert jsonld_offers([doc]) == [
        (False, 10.0, "https://schema.org/OutOfStock"), (True, 12.5, "https://schema.org/InStock")]


def test_jsonld_ignores_related_products():
    html = ld("https://schema.org/OutOfStock", extra={
        "isRelatedTo": [{"@type": "Product", "offers": {"availability": "https://schema.org/InStock", "price": 5}}]})
    assert REGISTRY["nintendo"].evaluate(page(html)).status is Status.OUT_OF_STOCK


def test_extract_survives_bad_jsonld_and_reads_meta():
    html = ('<title> A  B </title><script type="application/ld+json">{not json</script>'
            '<meta property="og:availability" content="instock"><meta property="product:price:amount" content="49.99">')
    ext = extract(html)
    assert ext.jsonld == [] and ext.title == "A B" and ext.meta["og:availability"] == "instock"


# ---- retailer evaluation ------------------------------------------------------

def test_nintendo_jsonld_out_and_in():
    r = REGISTRY["nintendo"]
    out = r.evaluate(page(ld("https://schema.org/OutOfStock", "49.99")))
    assert (out.status, out.price) == (Status.OUT_OF_STOCK, 49.99)
    assert r.evaluate(page(ld("https://schema.org/InStock", "49.99"))) == Verdict(Status.IN_STOCK, 49.99, "json-ld: InStock")


def test_hori_meta_and_microdata_fallback():
    r = REGISTRY["hori"]
    html = '<meta property="og:availability" content="oos"><meta property="product:price:amount" content="49.99">'
    assert r.evaluate(page(html)).status is Status.OUT_OF_STOCK
    html = '<link itemprop="availability" href="https://schema.org/InStock"><meta itemprop="price" content="49.99">'
    v = r.evaluate(page(html))
    assert (v.status, v.price) == (Status.IN_STOCK, 49.99)


def test_unreadable_page_is_unknown_not_out():
    v = REGISTRY["nintendo"].evaluate(page("<html><title>Thing</title><body>hello</body></html>"))
    assert v.status is Status.UNKNOWN


@pytest.mark.parametrize("kw", [
    dict(status=403), dict(status=429),
    dict(html="<title>Robot or human?</title>"), dict(html="<title>Access Denied</title>"),
    dict(html='<div id="px-captcha"></div>'),
    dict(html=ld("https://schema.org/InStock"), final_url="https://www.walmart.com/blocked?url=abc"),
])
def test_blocked_detection(kw):
    assert REGISTRY["walmart"].evaluate(page(**kw)).status is Status.BLOCKED


def test_normal_page_is_not_flagged_blocked():
    html = ld("https://schema.org/OutOfStock") + '<script src="/cdn-cgi/challenge-platform/x.js"></script>'
    assert REGISTRY["target"].evaluate(page(html)).status is Status.OUT_OF_STOCK


def test_fetch_failure_and_404_are_unknown():
    assert REGISTRY["hori"].evaluate(Page(url="u", status=0, error="timeout")).status is Status.UNKNOWN
    assert REGISTRY["hori"].evaluate(page("<title>Not found</title>", status=404)).status is Status.UNKNOWN


def test_bestbuy_button_beats_stale_jsonld():
    r = REGISTRY["bestbuy"]
    sel = "button[data-button-state]"
    sold = page(ld("https://schema.org/InStock"), elements={sel: [Element("Sold Out", "SOLD_OUT", True)]})
    assert r.evaluate(sold).status is Status.OUT_OF_STOCK
    live = page(ld("https://schema.org/OutOfStock"), elements={sel: [Element("Add to Cart", "ADD_TO_CART")]})
    v = r.evaluate(live)
    assert v.status is Status.IN_STOCK and v.price == 99.99 and "Add to Cart" in v.detail


def test_bestbuy_state_attribute_alone_and_hidden_and_disabled():
    r = REGISTRY["bestbuy"]
    sel = "button[data-button-state]"
    assert r.evaluate(page(elements={sel: [Element("", "PRE_ORDER")]})).status is Status.IN_STOCK
    assert r.evaluate(page(elements={sel: [Element("Add to Cart", visible=False)]})).status is Status.UNKNOWN
    assert r.evaluate(page(elements={sel: [Element("Add to Cart", disabled=True)]})).status is Status.OUT_OF_STOCK


def test_carousel_buttons_do_not_count():
    r = REGISTRY["bestbuy"]
    sel = "button[data-button-state]"
    els = [Element("Sold Out", "SOLD_OUT", True), Element("Add to Cart", "ADD_TO_CART")]
    assert r.evaluate(page(elements={sel: els})).status is Status.OUT_OF_STOCK


def test_bestbuy_falls_back_to_jsonld_without_buttons():
    assert REGISTRY["bestbuy"].evaluate(page(ld("https://schema.org/OutOfStock"))).status is Status.OUT_OF_STOCK


def test_target_shipping_wins_over_store_pickup_and_sold_out_block():
    r = REGISTRY["target"]
    p = page(elements={
        '[data-test="orderPickupButton"]': [Element("Out of stock at your store")],
        '[data-test="shippingButton"]': [Element("Add to cart")],
    })
    assert r.evaluate(p).status is Status.IN_STOCK
    p = page(ld("https://schema.org/InStock"), elements={'[data-test="soldOutBlock"]': [Element("Out of stock")]})
    assert r.evaluate(p).status is Status.OUT_OF_STOCK


def walmart_html(availability, price=99.99, seller="Walmart.com", nested=True) -> str:
    product = {"usItemId": "1", "availabilityStatus": availability, "sellerName": seller,
               "priceInfo": {"currentPrice": {"price": price}}}
    data = {"props": {"pageProps": {"initialData": {"data": {"product": product}}}}} if nested else {"x": [{"y": product}]}
    return f'<title>Thing - Walmart.com</title><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def test_walmart_next_data():
    r = REGISTRY["walmart"]
    v = r.evaluate(page(walmart_html("IN_STOCK")))
    assert (v.status, v.price) == (Status.IN_STOCK, 99.99)
    assert r.evaluate(page(walmart_html("OUT_OF_STOCK"))).status is Status.OUT_OF_STOCK
    assert r.evaluate(page(walmart_html("IN_STOCK", nested=False))).status is Status.IN_STOCK


def test_walmart_third_party_seller_is_not_a_restock():
    v = REGISTRY["walmart"].evaluate(page(walmart_html("IN_STOCK", 189.0, "Scalper LLC")))
    assert v.status is Status.OUT_OF_STOCK and "third-party" in v.detail


def test_price_cap():
    v = apply_price_cap(Verdict(Status.IN_STOCK, 189.0, "x"), 110.0)
    assert v.status is Status.OUT_OF_STOCK and "above" in v.detail
    assert apply_price_cap(Verdict(Status.IN_STOCK, 99.99, "x"), 110.0).status is Status.IN_STOCK
    assert apply_price_cap(Verdict(Status.IN_STOCK, None, "x"), 110.0).status is Status.IN_STOCK


# ---- state transitions --------------------------------------------------------

IN, OUT = Verdict(Status.IN_STOCK, 99.99), Verdict(Status.OUT_OF_STOCK)
UNK, BLK = Verdict(Status.UNKNOWN), Verdict(Status.BLOCKED)


def feed(entry, seq, start=1000.0, step=60.0, realert=600.0, after=3):
    events, now = [], start
    for v in seq:
        events.append(apply_verdict(entry, v, now, realert, after))
        now += step
    return events


def test_alert_only_on_transition_into_stock():
    assert feed(Entry(), [OUT, OUT, IN, IN, IN]) == [[], [], ["restock"], [], []]


def test_first_poll_in_stock_alerts():
    assert feed(Entry(), [IN]) == [["restock"]]


def test_unknown_and_blocked_do_not_cause_repeat_alerts():
    assert feed(Entry(), [IN, UNK, BLK, IN]) == [["restock"], [], [], []]


def test_realert_cooldown_then_new_restock():
    quick = feed(Entry(), [IN, OUT, IN], step=60.0)
    assert quick == [["restock"], [], []]
    slow = feed(Entry(), [IN, OUT, IN], step=400.0)
    assert slow == [["restock"], [], ["restock"]]


def test_parser_warning_once_per_streak():
    e = Entry()
    assert feed(e, [UNK, UNK, UNK, UNK]) == [[], [], ["parser"], []]
    assert feed(e, [OUT, UNK, UNK, UNK]) == [[], [], [], ["parser"]]


def test_blocked_does_not_count_as_unreadable():
    assert feed(Entry(), [BLK] * 6) == [[]] * 6


def test_state_roundtrip_and_corrupt_file(tmp_path):
    s = State()
    s.entry("a|b").definite = "in_stock"
    s.retailer("b").blocked_streak = 2
    s.last_heartbeat = 5.0
    path = tmp_path / "sub" / "state.json"
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.entries["a|b"].definite == "in_stock" and loaded.retailers["b"].blocked_streak == 2
    path.write_text("{broken")
    assert load_state(path).entries == {}
    assert load_state(tmp_path / "missing.json").entries == {}


# ---- config -------------------------------------------------------------------

def test_shipped_config_loads(tmp_path):
    cfg = load_config(ROOT / "deploy" / "config.toml", tmp_path)
    assert {p.id for p in cfg.products} == {"pro-controller", "pro-controller-stand", "electric-ocarina"}
    assert {k: v.fetch for k, v in cfg.retailers.items()} == {
        "nintendo": "http", "hori": "http", "bestbuy": "browser", "target": "browser", "walmart": "browser"}
    assert cfg.settings.state_path == tmp_path / "state.json"
    pairs = sorted((p.id, k) for p in cfg.products for k in p.urls)
    assert len(pairs) == 6


BASE = {"products": [{"id": "x", "urls": {"nintendo": "https://n.test/x"}}]}


def test_config_defaults_and_env():
    cfg = parse_config(BASE, env={"DISCORD_WEBHOOK_URL": " https://hook ", "TRACKER_DATA_DIR": "/d", "TRACKER_HEADLESS": "1"})
    assert cfg.webhook_url == "https://hook" and str(cfg.settings.data_dir) == "/d" and cfg.settings.headless
    assert cfg.retailers["nintendo"].fetch == "http"


def test_config_selector_override_and_fetch_switch():
    raw = dict(BASE, retailers={"nintendo": {"fetch": "browser", "buy_selectors": ["#buy"]}})
    rc = parse_config(raw, env={}).retailers["nintendo"]
    assert rc.fetch == "browser" and rc.retailer.buy_selectors == ("#buy",) and rc.min_interval == 75.0


@pytest.mark.parametrize("raw", [
    {},
    {"products": [{"id": "x", "urls": {}}]},
    {"products": [{"id": "x", "urls": {"amazon": "https://a.test"}}]},
    {"products": [{"id": "x", "urls": {"nintendo": "http://insecure.test"}}]},
    dict(BASE, retailers={"nintendo": {"min_interval": 2}}),
    dict(BASE, retailers={"nintendo": {"min_interval": 90, "max_interval": 30}}),
    dict(BASE, retailers={"nintendo": {"fetch": "carrier-pigeon"}}),
    dict(BASE, retailers={"nope": {}}),
    dict(BASE, settings={"typo_key": 1}),
    {"products": [{"id": "x", "urls": {"nintendo": "https://n.test"}}, {"id": "x", "urls": {"hori": "https://h.test"}}]},
])
def test_config_rejects_bad_input(raw):
    with pytest.raises(ConfigError):
        parse_config(raw, env={})


def test_blank_url_is_skipped():
    raw = {"products": [{"id": "x", "urls": {"nintendo": "https://n.test/x", "hori": ""}}]}
    assert list(parse_config(raw, env={}).retailers) == ["nintendo"]


# ---- notifier payloads --------------------------------------------------------

def test_allowed_mentions():
    assert allowed_mentions("@everyone") == {"parse": ["everyone"]}
    assert allowed_mentions("<@&123>") == {"roles": ["123"]}
    assert allowed_mentions("<@456>") == {"users": ["456"]}
    assert allowed_mentions("") == {"parse": []}


def test_notifier_without_webhook_reports_failure():
    assert Notifier("").send("t") is False


# ---- scheduler ----------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeFetcher:
    def __init__(self):
        self.pages = {}
        self.calls = []

    def fetch(self, url, selectors=()):
        self.calls.append(url)
        result = self.pages.get(url, page(ld("https://schema.org/OutOfStock")))
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.ok = True

    def send(self, title, description="", **kw):
        self.sent.append((title, kw.get("ping", False)))
        return self.ok


def make(tmp_path, raw=None):
    raw = raw or {
        "settings": {"unknown_alert_after": 2},
        "products": [{"id": "pad", "name": "Pad", "max_price": 110,
                      "urls": {"nintendo": "https://n.test/pad", "bestbuy": "https://b.test/pad"}}],
    }
    cfg = parse_config(raw, tmp_path, env={})
    clock, http, browser, notifier = FakeClock(), FakeFetcher(), FakeFetcher(), FakeNotifier()
    sched = Scheduler(cfg, {"http": http, "browser": browser}, notifier, State(),
                      clock=clock, sleep=clock.sleep, rng=random.Random(1))
    return sched, clock, http, browser, notifier


def run_for(sched, clock, seconds):
    end = clock.now + seconds
    while clock.now < end:
        sched.step()


def restocks(notifier):
    return [t for t, ping in notifier.sent if t.startswith("IN STOCK")]


def test_scheduler_polls_within_intervals_and_alerts_once(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    run_for(sched, clock, 600)
    assert 600 / 90 - 1 <= len(http.calls) <= 600 / 45 + 1
    assert 600 / 150 - 1 <= len(browser.calls) <= 600 / 75 + 1
    assert restocks(notifier) == []
    http.pages["https://n.test/pad"] = page(ld("https://schema.org/InStock"))
    run_for(sched, clock, 600)
    assert restocks(notifier) == ["IN STOCK: Pad"]
    assert ("IN STOCK: Pad", True) in notifier.sent
    assert load_state(tmp_path / "state.json").entries["pad|nintendo"].definite == "in_stock"


def test_failed_delivery_is_retried_next_poll(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    http.pages["https://n.test/pad"] = page(ld("https://schema.org/InStock"))
    notifier.ok = False
    run_for(sched, clock, 200)
    attempts = len(restocks(notifier))
    assert attempts >= 2
    notifier.ok = True
    run_for(sched, clock, 400)
    assert len(restocks(notifier)) == attempts + 1


def test_reseller_price_does_not_alert(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    http.pages["https://n.test/pad"] = page(ld("https://schema.org/InStock", "189.00"))
    run_for(sched, clock, 300)
    assert restocks(notifier) == []


def test_blocked_backs_off_and_warns_once(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    browser.pages["https://b.test/pad"] = page(status=403)
    run_for(sched, clock, 3600)
    assert len(browser.calls) <= 6  # 300s, 600s, 1200s, then capped at 1800s
    assert len([t for t, _ in notifier.sent if "blocking" in t]) == 1
    assert len(http.calls) > 30  # the other retailer keeps going
    browser.pages["https://b.test/pad"] = page(ld("https://schema.org/OutOfStock"))
    run_for(sched, clock, 2000)
    assert sched.state.retailer("bestbuy").blocked_streak == 0


def test_unreadable_page_warns_and_saves_debug_copy(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    http.pages["https://n.test/pad"] = page("<title>Pad</title><body>redesign</body>")
    run_for(sched, clock, 400)
    assert len([t for t, _ in notifier.sent if t.startswith("Can't read stock")]) == 1
    assert "redesign" in (tmp_path / "debug" / "nintendo-pad.html").read_text()


def test_fetcher_exception_does_not_kill_loop(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    http.pages["https://n.test/pad"] = RuntimeError("boom")
    run_for(sched, clock, 300)
    assert sched.state.entries["pad|nintendo"].status == "unknown" and len(browser.calls) >= 1


def test_pause_file_holds_browser_checks_only(tmp_path):
    sched, clock, http, browser, notifier = make(tmp_path)
    (tmp_path / "PAUSE").write_text("x")
    run_for(sched, clock, 400)
    assert browser.calls == [] and len(http.calls) >= 3
    (tmp_path / "PAUSE").unlink()
    run_for(sched, clock, 200)
    assert len(browser.calls) >= 1


def test_heartbeat_summary_and_liveness_file(tmp_path):
    raw = {"settings": {"heartbeat_hours": 1},
           "products": [{"id": "pad", "name": "Pad", "urls": {"nintendo": "https://n.test/pad"}}]}
    sched, clock, http, browser, notifier = make(tmp_path, raw)
    sched.announce_start()
    run_for(sched, clock, 2 * 3600 + 60)
    assert [t for t, _ in notifier.sent].count("Still watching") == 2
    assert "out of stock" in sched.summary()
    assert abs(int((tmp_path / "heartbeat").read_text()) - clock.now) < 30
