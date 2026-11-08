"""Channel3 client, the price-feed verifier, and the pricefeed arena scenarios (recorded mode, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from trustee import pricefeed
from trustee.agents.buyer import ArtifactStore, BuyerAgent
from trustee.arena.api import create_app
from trustee.arena.runner import DealRunner, default_checkers, default_counters
from trustee.arena.scenario import FIXTURES, get_scenario
from trustee.channel3 import (
    Channel3AuthError,
    Channel3Client,
    Channel3CreditsError,
    Channel3NotFound,
    Channel3RequestError,
    Channel3Unavailable,
    resolve_mode,
)
from trustee.ledger import Ledger
from trustee.paypal import PayPalClient
from trustee.protocol import Accept, Attest, Contract, Delivery, GoodSpec, PartialTerms

GOODS = FIXTURES / "goods"
HONEST = GOODS / "pricefeed-2026q4.csv"
STALE = GOODS / "pricefeed-2026q4-stale.csv"
MIXED = GOODS / "pricefeed-2026q4-mixed.csv"


def product_json(pid: str = "p1", offers: list[dict] | None = None) -> dict:
    return {
        "id": pid,
        "title": "Widget",
        "offers": offers
        if offers is not None
        else [
            {
                "url": "https://buy.example/x",
                "domain": "shop.example",
                "price": {"price": 19.99, "currency": "USD"},
                "availability": "InStock",
            }
        ],
    }


def live_client(handler) -> Channel3Client:
    return Channel3Client(api_key="k-secret", mode="live", transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------------ client


def test_search_parses_products_and_sends_key_and_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers["x-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"products": [product_json()]})

    client = live_client(handler)
    [product] = client.search("widget", limit=3)
    assert seen == {"path": "/v1/search", "key": "k-secret", "body": {"query": "widget", "limit": 3}}
    assert product.id == "p1"
    assert product.offers[0].domain == "shop.example"
    assert product.offers[0].price == Decimal("19.99")  # exact, not a binary float
    assert client.credits_used == 1


def test_offer_price_accepts_the_amount_spelling_from_the_guides():
    offer = {"url": "u", "domain": "WWW.Shop.example", "price": {"amount": 395, "currency": "usd"}}
    client = live_client(lambda r: httpx.Response(200, json={"product": product_json(offers=[offer])}))
    got = client.lookup("https://shop.example/p").offers[0]
    assert (got.domain, got.price, got.currency) == ("shop.example", Decimal("395"), "USD")


def test_lookup_posts_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"product": product_json()})

    assert live_client(handler).lookup("https://shop.example/w").id == "p1"
    assert seen == {"path": "/v1/lookup", "body": {"url": "https://shop.example/w"}}


def test_price_history_parses_statistics():
    body = {
        "canonical_product_id": "p1",
        "history": [{"timestamp": "2026-10-01T00:00:00Z", "price": 20.0, "currency": "USD"}],
        "statistics": {
            "mean": 21.5,
            "std_dev": 1.0,
            "min_price": 19.0,
            "max_price": 24.0,
            "current_price": 19.99,
            "current_status": "low",
            "currency": "USD",
        },
    }
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=body)

    history = live_client(handler).price_history("p1", days=30)
    assert seen["url"].endswith("/v0/price-tracking/history/p1?days=30")
    assert (history.current_status, history.current_price, history.points) == ("low", Decimal("19.99"), 1)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, Channel3AuthError),
        (402, Channel3CreditsError),
        (404, Channel3NotFound),
        (422, Channel3RequestError),
        (500, Channel3Unavailable),
        (504, Channel3Unavailable),
    ],
)
def test_http_errors_map_to_typed_errors(status, error):
    client = live_client(lambda r: httpx.Response(status, json={"detail": "nope"}))
    with pytest.raises(error) as caught:
        client.lookup("https://shop.example/w")
    assert caught.value.status == status
    assert "k-secret" not in str(caught.value)


def test_timeout_and_garbage_are_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(Channel3Unavailable):
        live_client(boom).search("x")
    with pytest.raises(Channel3Unavailable):
        live_client(lambda r: httpx.Response(200, text="<html>")).search("x")
    with pytest.raises(Channel3Unavailable):
        live_client(lambda r: httpx.Response(200, json={"nope": 1})).search("x")


def test_live_without_key_is_an_auth_error_and_repr_hides_the_key():
    with pytest.raises(Channel3AuthError):
        Channel3Client(api_key=None, mode="live").search("x")
    assert "k-secret" not in repr(Channel3Client(api_key="k-secret", mode="live"))


def test_mode_resolution(monkeypatch):
    monkeypatch.delenv("CHANNEL3_MODE", raising=False)
    assert resolve_mode(None, None) == "recorded"
    assert resolve_mode("k", None) == "live"
    assert resolve_mode("k", "recorded") == "recorded"
    monkeypatch.setenv("CHANNEL3_MODE", "recorded")
    assert resolve_mode("k", None) == "recorded"
    with pytest.raises(Channel3AuthError):
        resolve_mode(None, "live")


def test_from_env_reads_key_and_base_url(monkeypatch):
    monkeypatch.delenv("CHANNEL3_MODE", raising=False)
    monkeypatch.setenv("CHANNEL3_API_KEY", "abc")
    monkeypatch.setenv("CHANNEL3_BASE_URL", "https://example.test/")
    client = Channel3Client.from_env()
    assert (client.mode, client.base_url) == ("live", "https://example.test")
    monkeypatch.delenv("CHANNEL3_API_KEY")
    assert Channel3Client.from_env().mode == "recorded"


def test_recorded_mode_serves_fixtures_with_no_network_and_no_credits():
    client = Channel3Client(mode="recorded")
    assert client.lookup(next(iter(json.loads((FIXTURES / "channel3/lookup.json").read_text())))).offers
    title = next(iter(json.loads((FIXTURES / "channel3/search.json").read_text())))
    assert client.search(title)[0].title == title
    assert client.search("no such product") == []
    with pytest.raises(Channel3NotFound):
        client.lookup("https://nowhere.example/x")
    with pytest.raises(Channel3NotFound):
        client.price_history("c3-999")
    assert client.price_history("c3-001").current_status in {"low", "typical", "high"}
    assert client.credits_used == 0


# --------------------------------------------------------------------- sampling


def test_sample_is_deterministic_and_depends_on_the_digest():
    a = pricefeed.sample_indices("d" * 64, 40)
    assert a == pricefeed.sample_indices("d" * 64, 40)
    assert a == sorted(set(a)) and len(a) == pricefeed.SAMPLE_SIZE
    assert a != pricefeed.sample_indices("e" * 64, 40)
    assert pricefeed.sample_indices("d" * 64, 5) == [0, 1, 2, 3, 4]  # short feed: all rows


# -------------------------------------------------------------------- tolerance


@pytest.mark.parametrize(
    ("market", "feed", "ok"),
    [
        ("100.00", "105.00", True),  # exactly 5%
        ("100.00", "105.01", False),
        ("100.00", "95.00", True),
        ("100.00", "94.99", False),
        ("10.00", "10.99", True),  # the $1 floor wins under $20
        ("10.00", "11.00", True),
        ("10.00", "11.01", False),
        ("10.00", "9.00", True),
    ],
)
def test_tolerance_edges(market, feed, ok):
    offer = {"url": "u", "domain": "shop.example", "price": {"price": market, "currency": "USD"}}
    client = live_client(lambda r: httpx.Response(200, json={"product": product_json(offers=[offer])}))
    row = {
        "retailer": "shop.example",
        "product_url": "https://shop.example/w",
        "product_title": "W",
        "price": feed,
        "currency": "USD",
        "observed_at": "2026-10-31",
    }
    check, _ = pricefeed.check_row(client, 0, row)
    assert check.ok is ok
    assert check.reason == (pricefeed.OK if ok else pricefeed.OUT_OF_TOLERANCE)


def _row(**kw: str) -> dict[str, str]:
    base = {
        "retailer": "shop.example",
        "product_url": "https://shop.example/w",
        "product_title": "W",
        "price": "19.99",
        "currency": "USD",
        "observed_at": "2026-10-31",
    }
    return base | kw


def test_row_failure_reasons():
    ok_client = live_client(lambda r: httpx.Response(200, json={"product": product_json()}))
    assert pricefeed.check_row(ok_client, 0, _row())[0].ok
    assert pricefeed.check_row(ok_client, 0, _row(retailer="other.example"))[0].reason == pricefeed.NO_OFFER
    assert pricefeed.check_row(ok_client, 0, _row(currency="EUR"))[0].reason == pricefeed.CURRENCY
    for bad in ("", "abc", "-1", "0", "NaN"):
        assert pricefeed.check_row(ok_client, 0, _row(price=bad))[0].reason == pricefeed.MALFORMED
    gone = live_client(lambda r: httpx.Response(404, json={"detail": "Product not found"}))
    assert pricefeed.check_row(gone, 0, _row())[0].reason == pricefeed.NOT_FOUND
    notpage = live_client(lambda r: httpx.Response(422, json={"detail": "URL is not a product page"}))
    assert pricefeed.check_row(notpage, 0, _row())[0].reason == pricefeed.NOT_FOUND


def test_title_only_row_goes_through_search_and_matches_the_retailer():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        return httpx.Response(200, json={"products": [product_json()]})

    check, _ = pricefeed.check_row(live_client(handler), 0, _row(product_url=""))
    assert check.ok


# ------------------------------------------------------------- feed verification

GOOD = GoodSpec(
    good_id=pricefeed.GOOD_ID,
    description="price feed",
    category="data",
    acceptance_criteria=["sha256 matches contract", pricefeed.CRITERION],
    deliverable_ref="pricefeed-2026q4.csv",
)


def make_contract(ref: str, partial: PartialTerms | None = None) -> Contract:
    accept = Accept(
        sender="buyer-agent-1",
        responds_to="c1",
        good=GOOD.model_copy(update={"deliverable_ref": ref}),
        price=Decimal("156.00"),
        deadline=datetime.now() + timedelta(hours=1),
    )
    contract = Contract.from_accept(accept, seller="seller-agent-1")
    return contract.model_copy(update={"partial": partial}) if partial else contract


def recorded_verifier(**kw) -> pricefeed.PriceFeedVerifier:
    return pricefeed.PriceFeedVerifier(lambda: Channel3Client(mode="recorded"), **kw)


def test_honest_feed_passes():
    ok, detail = recorded_verifier()(HONEST, GOOD.good_id, contract=make_contract(HONEST.name))
    assert ok, detail
    assert "20 of 20 sampled rows" in detail


def test_stale_feed_fails_with_the_failed_marker():
    ok, detail = recorded_verifier()(STALE, GOOD.good_id, contract=make_contract(STALE.name))
    assert not ok
    assert detail.startswith("failed: market check")
    assert "rows" in detail  # the arbiter's row clause


def test_mixed_feed_fails_the_check_but_counts_valid_sampled_rows():
    verifier = recorded_verifier()
    contract = make_contract(MIXED.name)
    ok, _ = verifier(MIXED, GOOD.good_id, contract=contract)
    valid, total = verifier.counter(MIXED, GOOD.good_id, contract=contract)
    assert total == pricefeed.SAMPLE_SIZE
    assert valid >= total * 0.6  # 8 bad rows in 40 cap the damage in any sample of 20
    assert ok == (valid == total)


def test_counts_match_an_independently_computed_sample():
    contract = make_contract(MIXED.name)
    bad_rows = {i for i in range(40) if i % 5 == 4}  # how build_pricefeed makes the mixed feed
    sample = pricefeed.sample_indices(contract.digest(), 40)
    valid, total = recorded_verifier().counter(MIXED, GOOD.good_id, contract=contract)
    assert total == len(sample)
    assert valid == len([i for i in sample if i not in bad_rows])


def test_one_pass_over_channel3_feeds_checker_counter_and_annotation():
    calls: list[int] = []

    def factory() -> Channel3Client:
        client = Channel3Client(mode="recorded")
        calls.append(1)
        return client

    verifier = pricefeed.PriceFeedVerifier(factory)
    contract = make_contract(MIXED.name)
    verifier(MIXED, GOOD.good_id, contract=contract)
    verifier.counter(MIXED, GOOD.good_id, contract=contract)
    verifier.annotate(MIXED, contract)
    assert len(calls) == 1


def test_bad_header_and_missing_file_fail_without_counts(tmp_path: Path):
    bad = tmp_path / "f.csv"
    bad.write_text("a,b\n1,2\n")
    verifier = recorded_verifier()
    contract = make_contract("f.csv")
    ok, detail = verifier(bad, GOOD.good_id, contract=contract)
    assert not ok and detail.startswith("failed: schema mismatch")
    assert verifier.counter(bad, GOOD.good_id, contract=contract) == (0, 0)
    ok, detail = verifier(tmp_path / "missing.csv", GOOD.good_id, contract=contract)
    assert not ok and "failed" in detail


def test_channel3_outage_is_unverified_not_a_seller_breach():
    def factory() -> Channel3Client:
        return live_client(lambda r: httpx.Response(402, json={"detail": "You have used all of your free credits."}))

    verifier = pricefeed.PriceFeedVerifier(factory)
    contract = make_contract(HONEST.name)
    ok, detail = verifier(HONEST, GOOD.good_id, contract=contract)
    assert not ok
    assert detail.startswith("unverified:") and "failed" not in detail
    assert verifier.counter(HONEST, GOOD.good_id, contract=contract) == (0, 0)


def test_needs_the_contract():
    with pytest.raises(ValueError):
        recorded_verifier()(HONEST, GOOD.good_id)


def test_fair_price_note_is_opt_in():
    contract = make_contract(HONEST.name)
    off = recorded_verifier(fair_price=False).annotate(HONEST, contract)[0]
    on = recorded_verifier(fair_price=True).annotate(HONEST, contract)
    assert "market_fair_price" not in off
    assert "Channel3 history" in on[0]["market_fair_price"]
    assert on[1][-1]["event"] == "fair_price"


def test_live_credit_cost_is_one_per_sampled_row():
    products = json.loads((FIXTURES / "channel3/lookup.json").read_text())
    search = json.loads((FIXTURES / "channel3/search.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/lookup":
            hit = products.get(body["url"])
            return httpx.Response(200, json={"product": hit}) if hit else httpx.Response(404, json={"detail": "x"})
        return httpx.Response(200, json={"products": search.get(body["query"], [])})

    holder: list[Channel3Client] = []

    def factory() -> Channel3Client:
        holder.append(live_client(handler))
        return holder[0]

    verifier = pricefeed.PriceFeedVerifier(factory)
    contract = make_contract(HONEST.name)
    ok, detail = verifier(HONEST, GOOD.good_id, contract=contract)
    assert ok, detail
    assert holder[0].credits_used == pricefeed.SAMPLE_SIZE
    assert "live Channel3" in detail


# ----------------------------------------------------------- buyer integration


class NoLLM:
    def complete_json(self, *args, **kwargs):
        raise RuntimeError("no model in this test")


def make_buyer(tmp_path: Path, ledger: Ledger | None = None) -> tuple[BuyerAgent, ArtifactStore]:
    store = ArtifactStore(goods_dir=GOODS, manifest={}, inbox=tmp_path)
    buyer = BuyerAgent(
        agent_id="buyer-agent-1",
        llm=NoLLM(),  # type: ignore[arg-type]
        good=GOOD,
        budget=Decimal("165"),
        store=store,
        checkers={GOOD.good_id: recorded_verifier()},
        counters={GOOD.good_id: recorded_verifier().counter},
        ledger=ledger,
    )
    return buyer, store


def deliver(store: ArtifactStore, contract: Contract) -> Delivery:
    ref = contract.good.deliverable_ref
    digest = store.receive(ref, (GOODS / ref).read_bytes())
    return Delivery(sender="seller-agent-1", deal_id=contract.deal_id, deliverable_ref=ref, content_hash=digest)


def contract_with_hash(ref: str, partial: PartialTerms | None = None) -> Contract:
    import hashlib

    contract = make_contract(ref, partial)
    sha = hashlib.sha256((GOODS / ref).read_bytes()).hexdigest()
    return contract.model_copy(update={"good": contract.good.model_copy(update={"sha256": sha})})


def test_buyer_attests_accept_with_evidence_and_tape_rows(tmp_path: Path):
    ledger = Ledger()
    buyer, store = make_buyer(tmp_path, ledger)
    contract = contract_with_hash(HONEST.name)
    attest = buyer.verify_delivery(contract, deliver(store, contract))
    assert attest.verdict == "accept"
    assert attest.evidence["market_source"] == "recorded"
    assert attest.evidence["market_rows_sampled"] == "20"
    rows = [e for e in ledger.tail(contract.deal_id) if e.payload.get("event") == "market_check"]
    assert len(rows) == 20 and all(r.payload["ok"] for r in rows)


def test_buyer_rejects_stale_feed_and_reports_counts_only_with_a_partial_term(tmp_path: Path):
    buyer, store = make_buyer(tmp_path)
    plain = contract_with_hash(STALE.name)
    attest = buyer.verify_delivery(plain, deliver(store, plain))
    assert attest.verdict == "reject"
    assert "units_valid" not in attest.evidence
    with_term = contract_with_hash(STALE.name, PartialTerms(basis="units", min_fraction=Decimal("0.5")))
    attest = buyer.verify_delivery(with_term, deliver(store, with_term))
    assert attest.verdict == "reject"
    assert attest.evidence["units_total"] == "20"


def test_buyer_mixed_feed_with_partial_term_yields_a_prorata_payout(tmp_path: Path):

    buyer, store = make_buyer(tmp_path)
    contract = contract_with_hash(MIXED.name, PartialTerms(basis="units", min_fraction=Decimal("0.5")))
    attest = buyer.verify_delivery(contract, deliver(store, contract))
    from trustee.partial import compute_partial, prorata_amount

    settlement = compute_partial(contract, attest.evidence)
    if attest.verdict == "accept":  # no bad row happened to be sampled
        assert settlement is None
    else:
        valid, total = int(attest.evidence["units_valid"]), int(attest.evidence["units_total"])
        assert settlement is not None
        assert Decimal(settlement.captured) == prorata_amount(Decimal("156.00"), valid, total)


# ---------------------------------------------------------------- signing bytes


def test_no_signed_model_gained_a_field():
    """The price-feed check adds evidence strings only; the signed wire models are exactly as before."""
    assert sorted(Contract.model_fields) == [
        "buyer", "currency", "deadline", "deal_id", "formed_at", "good", "partial", "price", "seller",
        "signatures", "transcript_ref",
    ]  # fmt: skip
    assert sorted(Attest.model_fields) == [
        "deal_id", "evidence", "kind", "msg_id", "reason", "sender", "signature", "ts", "verdict",
    ]  # fmt: skip
    contract = make_contract(HONEST.name)
    assert "partial" not in contract.model_dump(mode="json", exclude={"signatures", "transcript_ref", "partial"})


def test_scenario_criteria_name_the_check_and_hashes_match_the_files():
    import hashlib

    for name, path in (("pricefeed", HONEST), ("pricefeed-stale", STALE), ("pricefeed-mixed", MIXED)):
        sc = get_scenario(name)
        assert pricefeed.CRITERION in sc.good.acceptance_criteria
        assert sc.good.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert get_scenario("pricefeed-mixed").partial is not None
    assert get_scenario("pricefeed").partial is None


def test_checkers_and_counters_are_registered_for_the_good():
    assert pricefeed.GOOD_ID in default_checkers()
    assert pricefeed.GOOD_ID in default_counters()


# ------------------------------------------------------------------- scenarios


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)
    monkeypatch.setenv("CHANNEL3_MODE", "recorded")
    return DealRunner(ledger=Ledger(), paypal=PayPalClient(dry_run=True))


def test_pricefeed_scenario_settles(runner):
    result = runner.run(get_scenario("pricefeed"), mode="scripted")
    assert result.state == "SETTLED"
    assert result.attest["evidence"]["market_rows_valid"] == "20"


def test_pricefeed_stale_scenario_voids_citing_the_market_clause(runner):
    result = runner.run(get_scenario("pricefeed-stale"), mode="scripted")
    assert result.state == "VOIDED"
    assert pricefeed.CRITERION in result.ruling["clause"]


def test_pricefeed_mixed_scenario_settles_pro_rata(runner):
    result = runner.run(get_scenario("pricefeed-mixed"), mode="scripted")
    assert result.state == "SETTLED"  # a pro-rata capture, or a full one in the rare case no bad row was sampled
    assert result.attest["evidence"]["units_total"] == "20" or result.attest["verdict"] == "accept"


def test_pricefeed_scenarios_are_listed_only_with_the_flag(monkeypatch):
    def names() -> list[str]:
        app = create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
        return [s["name"] for s in TestClient(app).get("/api/scenarios").json()["scenarios"]]

    monkeypatch.delenv("ARENA_PRICEFEED_SCENARIO", raising=False)
    monkeypatch.delenv("ARENA_PARTIAL_SCENARIO", raising=False)
    without = names()
    assert not any(n.startswith("pricefeed") for n in without)
    monkeypatch.setenv("ARENA_PRICEFEED_SCENARIO", "1")
    with_flag = names()
    assert {"pricefeed", "pricefeed-stale", "pricefeed-mixed"} <= set(with_flag)
    assert [n for n in with_flag if not n.startswith("pricefeed")] == without
    assert "partial" not in with_flag  # the partial flag is separate


def test_northwind_on_the_a2a_path_gets_the_same_checker_and_counter():
    from trustee.network import create_northwind

    northwind = create_northwind(good=get_scenario("pricefeed").good)
    assert northwind.checkers[pricefeed.GOOD_ID].needs_contract
    assert northwind.counters[pricefeed.GOOD_ID].needs_contract
