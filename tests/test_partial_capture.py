"""Partial capture (pro-rata settlement) and authorization renewal."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from trustee.agents.trustee import Trustee
from trustee.identity import AgentKey
from trustee.ledger import Ledger
from trustee.mandate import Mandate, MandateEngine, demo_mandates
from trustee.partial import compute_partial, prorata_amount
from trustee.paypal import EscrowOrder, PayPalClient, PayPalError
from trustee.protocol import Accept, Attest, Contract, Delivery, GoodSpec, PartialTerms
from trustee.state import Deal, DealState
from trustee.sweeper import DeadlineSweeper

GOOD = GoodSpec(
    good_id="returns-dataset-2026q3",
    description="Q3 2026 returns dataset",
    category="data",
    acceptance_criteria=["rows pass the buyer's checks"],
    deliverable_ref="returns.csv",
)


def make_contract(price: str = "156.00", partial: PartialTerms | None = None, hours: float = 1) -> Contract:
    accept = Accept(
        sender="buyer-agent-1",
        responds_to="c1",
        good=GOOD,
        price=Decimal(price),
        deadline=datetime.now() + timedelta(hours=hours),
    )
    contract = Contract.from_accept(accept, seller="seller-agent-1")
    return contract.model_copy(update={"partial": partial}) if partial is not None else contract


HALF = PartialTerms(basis="units", min_fraction=Decimal("0.5"))


def make_trustee(contract: Contract, paypal: PayPalClient | None = None, mandates: MandateEngine | None = None):
    deal = Deal(contract.deal_id)
    deal.transition(DealState.NEGOTIATING, "t")
    ledger = Ledger()
    engine = mandates or MandateEngine(demo_mandates())
    return Trustee(deal, engine, paypal or PayPalClient(dry_run=True), ledger=ledger), engine, ledger


def deliver(trustee: Trustee, contract: Contract) -> None:
    assert trustee.on_contract(contract).action == "escrowed"
    d = Delivery(deal_id=contract.deal_id, sender="seller-agent-1", deliverable_ref="returns.csv", content_hash="aa")
    assert trustee.on_delivery(d, contract).action == "delivered"


def reject(contract: Contract, **evidence: str) -> Attest:
    return Attest(
        deal_id=contract.deal_id, sender="buyer-agent-1", verdict="reject", reason="rows failed", evidence=evidence
    )


# ------------------------------------------------------------------ PayPal client


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reauth_status = 201

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "CAP1", "status": "COMPLETED", "final_capture": True})
        if path.endswith("/reauthorize"):
            if self.reauth_status != 201:
                return httpx.Response(self.reauth_status, json={"name": "AUTH_ERROR"})
            return httpx.Response(201, json={"id": "AUTH-NEW", "status": "CREATED"})
        if path.endswith("/void"):
            return httpx.Response(204)
        return httpx.Response(404)


def live(rec: Recorder) -> PayPalClient:
    client = PayPalClient(client_id="id", client_secret="s", dry_run=False, max_retries=0)
    client._http = httpx.Client(transport=httpx.MockTransport(rec.handler))
    return client


def authorized(authz: str = "AUTH-OLD") -> EscrowOrder:
    return EscrowOrder(order_id="ORD", authorization_id=authz, status="authorized")


def test_full_capture_request_is_unchanged_empty_body():
    rec = Recorder()
    live(rec).capture(authorized(), request_id="rid")
    req = rec.requests[-1]
    assert req.url.path == "/v2/payments/authorizations/AUTH-OLD/capture"
    assert req.content == b""
    assert req.headers["PayPal-Request-Id"] == "rid"


def test_partial_capture_request_carries_amount_and_final_capture():
    rec = Recorder()
    order = live(rec).capture(authorized(), amount=Decimal("140.40"), currency="USD", request_id="rid")
    req = rec.requests[-1]
    assert json.loads(req.content) == {"amount": {"currency_code": "USD", "value": "140.40"}, "final_capture": True}
    assert req.headers["PayPal-Request-Id"] == "rid"
    assert order.status == "captured"


def test_non_final_partial_capture():
    rec = Recorder()
    live(rec).capture(authorized(), amount=Decimal("10.00"), final_capture=False, request_id="r")
    assert json.loads(rec.requests[-1].content)["final_capture"] is False


@pytest.mark.parametrize("bad", ["0", "-1.00", "1.005"])
def test_capture_rejects_non_cent_or_non_positive_amounts(bad):
    with pytest.raises(ValueError):
        live(Recorder()).capture(authorized(), amount=Decimal(bad))


def test_dry_run_partial_capture_records_amount():
    order = PayPalClient(dry_run=True).capture(authorized(), amount=Decimal("5.50"), currency="EUR")
    assert order.details["captured_amount"] == {"currency_code": "EUR", "value": "5.50"}
    assert order.details["capture_id"].startswith("dry-cap-")


def test_reauthorize_swaps_authorization_id_for_later_calls():
    rec = Recorder()
    client = live(rec)
    order = client.reauthorize(authorized(), request_id="re-1")
    assert rec.requests[-1].url.path == "/v2/payments/authorizations/AUTH-OLD/reauthorize"
    assert rec.requests[-1].headers["PayPal-Request-Id"] == "re-1"
    assert order.authorization_id == "AUTH-NEW"
    assert order.details["previous_authorization_id"] == "AUTH-OLD"
    client.capture(order, request_id="c")
    assert rec.requests[-1].url.path == "/v2/payments/authorizations/AUTH-NEW/capture"


def test_reauthorize_failure_raises_and_keeps_the_old_id():
    rec = Recorder()
    rec.reauth_status = 422
    order = authorized()
    with pytest.raises(PayPalError):
        live(rec).reauthorize(order)
    assert order.authorization_id == "AUTH-OLD"


def test_dry_run_reauthorize_changes_the_id():
    order = PayPalClient(dry_run=True).reauthorize(authorized())
    assert order.authorization_id != "AUTH-OLD" and order.authorization_id.startswith("dry-authz-")


# ------------------------------------------------------------------ the math


@pytest.mark.parametrize(
    ("price", "valid", "total", "expected"),
    [
        ("156.00", 4500, 5000, "140.40"),
        ("10.00", 1, 3, "3.33"),  # rounds down, never up
        ("10.00", 2, 3, "6.66"),
        ("0.05", 1, 3, "0.01"),
        ("100.00", 1, 1000, "0.10"),
    ],
)
def test_prorata_rounds_down_to_the_cent(price, valid, total, expected):
    assert prorata_amount(Decimal(price), valid, total) == Decimal(expected)


def ev(valid: str, total: str) -> dict[str, str]:
    return {"units_valid": valid, "units_total": total}


def test_compute_partial_happy_path():
    p = compute_partial(make_contract(partial=HALF), ev("4500", "5000"))
    assert p is not None
    assert (p.captured, p.released, p.price, p.currency) == ("140.40", "15.60", "156.00", "USD")
    assert p.fraction == "0.9" and p.label() == "settled 90% (140.40 of 156.00 USD)"


def test_min_fraction_boundary_is_inclusive():
    contract = make_contract(partial=HALF)
    assert compute_partial(contract, ev("50", "100")) is not None
    assert compute_partial(contract, ev("49", "100")) is None


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        ev("0", "100"),  # nothing valid
        ev("100", "100"),  # all valid: a reject over something else is not a partial case
        ev("101", "100"),
        ev("5", "0"),
        ev("-5", "100"),
        ev("5.5", "100"),
        ev("abc", "100"),
        ev(" 5", "100"),
        ev("５", "100"),  # non-ASCII digit
        ev("1", "10" + "0" * 20),  # absurd total
        {"units_valid": "5"},
    ],
)
def test_malformed_or_unqualifying_evidence_is_not_partial(evidence):
    assert compute_partial(make_contract(partial=HALF), evidence) is None


def test_no_term_means_no_partial():
    assert compute_partial(make_contract(), ev("90", "100")) is None


def test_amount_rounding_to_zero_is_not_partial():
    contract = make_contract(price="0.01", partial=PartialTerms(basis="units", min_fraction=Decimal("0.0001")))
    assert compute_partial(contract, ev("1", "3")) is None


# ------------------------------------------------------------------ signing bytes


def test_contract_digest_unchanged_when_partial_is_unset():
    contract = make_contract()
    legacy_body = contract.model_dump(mode="json", exclude={"signatures", "transcript_ref", "partial"})
    import hashlib

    from trustee.identity import canonical_json

    assert contract.digest() == hashlib.sha256(canonical_json(legacy_body)).hexdigest()
    assert "partial" not in contract.key_clauses()


def test_old_signed_contract_still_verifies():
    from trustee.identity import AgentRecord, AgentRegistry

    key = AgentKey.generate()
    registry = AgentRegistry()
    registry.register(
        AgentRecord("buyer-agent-1", key.public_key_b64, "p", frozenset({"buyer"}), None),
    )
    signed = make_contract().sign("buyer-agent-1", key)
    # Round-trip through JSON that predates the field: no `partial` key at all.
    raw = signed.model_dump(mode="json")
    raw.pop("partial")
    revived = Contract.model_validate(raw)
    assert revived.partial is None
    assert revived.verify_party("buyer-agent-1", registry)


def test_partial_term_changes_the_digest_and_is_signed():
    base = make_contract()
    with_term = base.model_copy(update={"partial": HALF})
    assert with_term.digest() != base.digest()
    assert with_term.key_clauses()["partial"].startswith("units")


# ------------------------------------------------------------------ the money path


def test_partial_settlement_end_to_end():
    contract = make_contract(partial=HALF)
    trustee, mandates, ledger = make_trustee(contract)
    deliver(trustee, contract)
    d = trustee.on_attest(reject(contract, **ev("4500", "5000")), contract)
    assert d.action == "settled" and trustee.deal.state == DealState.SETTLED
    assert d.escrow is not None and d.escrow.details["captured_amount"]["value"] == "140.40"
    assert d.escrow.details["final_capture"] is True
    assert trustee.settlement is not None and trustee.settlement["released"] == "15.60"
    settled = [e for e in ledger.tail(contract.deal_id) if e.kind == "settlement"]
    assert len(settled) == 1
    assert settled[0].payload["captured"] == "140.40" and settled[0].payload["basis"] == "units"
    assert "settled 90%" in settled[0].payload["note"]
    # snapshot round-trips the settlement
    snap = trustee.snapshot()
    assert snap["settlement"]["captured"] == "140.40"
    again = Trustee.from_snapshot(snap, mandates=mandates, paypal=PayPalClient(dry_run=True))
    assert again.settlement == snap["settlement"]


def test_partial_settlement_accounts_only_for_the_captured_amount():
    contract = make_contract(partial=HALF)
    mandates = MandateEngine(demo_mandates())
    trustee, _, _ = make_trustee(contract, mandates=mandates)
    deliver(trustee, contract)
    trustee.on_attest(reject(contract, **ev("4500", "5000")), contract)
    assert mandates.committed("buyer-agent-1") == Decimal("140.40")


def test_partial_accounting_survives_a_budget_check():
    """The 15.60 that went back to the buyer is available for the next deal's budget."""
    buyer = demo_mandates()["buyer-agent-1"]
    capped = Mandate(**{**buyer.__dict__, "max_total": Decimal("200.00")})
    mandates = MandateEngine({**demo_mandates(), "buyer-agent-1": capped})
    contract = make_contract(partial=HALF)
    trustee, _, _ = make_trustee(contract, mandates=mandates)
    deliver(trustee, contract)
    trustee.on_attest(reject(contract, **ev("4500", "5000")), contract)
    nxt = make_contract(price="59.60")
    assert mandates.check_deal(nxt).ok
    assert not mandates.check_deal(make_contract(price="59.61")).ok


def test_below_min_fraction_follows_the_old_reject_path():
    contract = make_contract(partial=HALF)
    trustee, _, _ = make_trustee(contract)
    deliver(trustee, contract)
    d = trustee.on_attest(reject(contract, sha256_check="mismatch", **ev("10", "100")), contract)
    assert d.action == "voided" and trustee.settlement is None


def test_malformed_evidence_follows_the_old_reject_path():
    contract = make_contract(partial=HALF)
    trustee, _, _ = make_trustee(contract)
    deliver(trustee, contract)
    d = trustee.on_attest(reject(contract, sha256_check="mismatch", **ev("lots", "100")), contract)
    assert d.action == "voided"


def test_contract_without_the_term_ignores_counts():
    contract = make_contract()
    trustee, _, _ = make_trustee(contract)
    deliver(trustee, contract)
    d = trustee.on_attest(reject(contract, sha256_check="mismatch", **ev("90", "100")), contract)
    assert d.action == "voided" and trustee.settlement is None


class FlakyCapture(PayPalClient):
    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.calls: list[tuple[Decimal | None, bool]] = []
        self.fail = True

    def capture(self, order, *, amount=None, currency="USD", final_capture=True, request_id=None):
        self.calls.append((amount, final_capture))
        if self.fail:
            self.fail = False
            raise PayPalError("boom", 500)
        return super().capture(order, amount=amount, currency=currency, final_capture=final_capture)


def test_partial_capture_failure_keeps_funds_held_and_retry_is_the_same_call():
    contract = make_contract(partial=HALF)
    paypal = FlakyCapture()
    trustee, _, _ = make_trustee(contract, paypal=paypal)
    deliver(trustee, contract)
    attest = reject(contract, **ev("4500", "5000"))
    assert trustee.on_attest(attest, contract).action == "error"
    assert trustee.deal.state == DealState.DELIVERED and trustee.settlement is None
    assert trustee.on_attest(attest, contract).action == "settled"
    assert paypal.calls == [(Decimal("140.40"), True)] * 2


def test_mandate_revoked_before_partial_capture_holds_the_funds():
    contract = make_contract(partial=HALF)
    trustee, mandates, _ = make_trustee(contract)
    deliver(trustee, contract)
    mandates.revoke("buyer-agent-1")
    d = trustee.on_attest(reject(contract, **ev("4500", "5000")), contract)
    assert d.action == "disputed" and trustee.deal.state == DealState.DISPUTED and trustee.settlement is None


def test_service_view_and_a2a_receipt_show_the_settlement():
    from trustee.a2a.tasks import deal_task, receipt_data
    from trustee.platform.service import DealService

    contract = make_contract(partial=HALF)
    trustee, _, _ = make_trustee(contract)
    deliver(trustee, contract)
    trustee.on_attest(reject(contract, **ev("4500", "5000")), contract)
    view = DealService._view(trustee, contract)
    assert view["settlement"]["captured"] == "140.40" and view["contract"]["partial"]
    record = {"snapshot": trustee.snapshot(), "contract": contract.model_dump(mode="json"), "updated_at": None}
    receipt = receipt_data(record["snapshot"], record["contract"])
    assert receipt["amount"] == "140.40" and receipt["contractAmount"] == "156.00" and receipt["released"] == "15.60"
    task = deal_task(record)
    assert "Settled pro-rata: 140.40 USD of 156.00 USD" in task.status.message.model_dump_json()
    assert task.metadata["trustee"]["settlement"]["captured"] == "140.40"


def test_plain_settlement_snapshot_and_view_have_no_settlement_key():
    from trustee.platform.service import DealService

    contract = make_contract()
    trustee, _, _ = make_trustee(contract)
    deliver(trustee, contract)
    att = Attest(deal_id=contract.deal_id, sender="buyer-agent-1", verdict="accept", reason="ok")
    assert trustee.on_attest(att, contract).action == "settled"
    assert "settlement" not in trustee.snapshot()
    assert "settlement" not in DealService._view(trustee, contract)
    assert "reauth_count" not in trustee.snapshot()


# ------------------------------------------------------------------ reauthorize in the sweeper

T0 = datetime(2026, 1, 1, 12, 0)


def escrowed(deadline_days: float = 20):
    contract = make_contract()
    contract = contract.model_copy(update={"deadline": T0 + timedelta(days=deadline_days)})
    trustee, mandates, ledger = make_trustee(contract)
    assert trustee.on_contract(contract, now=T0).action == "escrowed"
    return trustee, contract, ledger


def test_sweeper_reauthorize_is_off_by_default(monkeypatch):
    monkeypatch.delenv("TRUSTEE_REAUTHORIZE", raising=False)
    trustee, contract, _ = escrowed()
    before = trustee.snapshot()["escrow"]["authorization_id"]
    sweeper = DeadlineSweeper()
    sweeper.register(trustee, contract)
    assert sweeper.tick(T0 + timedelta(days=5)) == []
    assert trustee.snapshot()["escrow"]["authorization_id"] == before


def test_sweeper_reauthorizes_after_the_honor_period_once_per_period(monkeypatch):
    monkeypatch.setenv("TRUSTEE_REAUTHORIZE", "1")
    trustee, contract, ledger = escrowed()
    first = trustee.snapshot()["escrow"]["authorization_id"]
    sweeper = DeadlineSweeper()
    assert sweeper.reauthorize is True
    sweeper.register(trustee, contract)

    assert sweeper.tick(T0 + timedelta(days=2)) == []  # still inside the honor period
    out = sweeper.tick(T0 + timedelta(days=4))
    assert [a.action for _, a in out] == ["reauthorized"]
    second = trustee.snapshot()["escrow"]["authorization_id"]
    assert second != first and trustee.deal.state == DealState.ESCROWED

    assert sweeper.tick(T0 + timedelta(days=5)) == []  # same period: nothing
    out = sweeper.tick(T0 + timedelta(days=7, minutes=1))
    assert [a.action for _, a in out] == ["reauthorized"]
    assert trustee.snapshot()["escrow"]["authorization_id"] not in (first, second)
    rows = [e for e in ledger.tail(contract.deal_id) if e.kind == "paypal" and e.payload.get("op") == "reauthorize"]
    assert len(rows) == 2 and rows[0].payload["previous_authorization_id"] == first


def test_reauthorize_not_after_the_deadline_or_the_29_day_window():
    trustee, contract, _ = escrowed(deadline_days=3.5)
    assert trustee.reauthorize(contract, T0 + timedelta(days=4)).action == "no-op"  # deadline passed
    trustee, contract, _ = escrowed(deadline_days=60)
    assert trustee.reauthorize(contract, T0 + timedelta(days=30)).action == "no-op"


def test_reauthorize_failure_keeps_deadline_void_behaviour(monkeypatch):
    class Failing(PayPalClient):
        def reauthorize(self, order, *, request_id=None):
            raise PayPalError("nope", 422)

    contract = make_contract().model_copy(update={"deadline": T0 + timedelta(days=6)})
    trustee, _, ledger = make_trustee(contract, paypal=Failing(dry_run=True))
    trustee.on_contract(contract, now=T0)
    sweeper = DeadlineSweeper(reauthorize=True)
    sweeper.register(trustee, contract)
    assert [a.action for _, a in sweeper.tick(T0 + timedelta(days=4))] == ["error"]
    assert trustee.deal.state == DealState.ESCROWED
    # backoff: the next sweep minutes later does not hammer PayPal
    assert sweeper.tick(T0 + timedelta(days=4, minutes=5)) == []
    # the deadline still voids
    out = sweeper.tick(T0 + timedelta(days=7))
    assert [a.action for _, a in out] == ["voided"]
    assert any("reauthorize" in str(e.payload.get("op")) for e in ledger.tail(contract.deal_id))


def test_capture_after_reauthorize_uses_the_new_authorization():
    class Spy(PayPalClient):
        seen: list[str | None] = []

        def capture(self, order, **kw):
            Spy.seen.append(order.authorization_id)
            return super().capture(order, **kw)

    contract = make_contract().model_copy(update={"deadline": T0 + timedelta(days=20)})
    trustee, _, _ = make_trustee(contract, paypal=Spy(dry_run=True))
    trustee.on_contract(contract, now=T0)
    trustee.reauthorize(contract, T0 + timedelta(days=4))
    new = trustee.snapshot()["escrow"]["authorization_id"]
    d = Delivery(deal_id=contract.deal_id, sender="seller-agent-1", deliverable_ref="returns.csv", content_hash="a")
    trustee.on_delivery(d, contract)
    trustee.on_attest(Attest(deal_id=contract.deal_id, sender="buyer-agent-1", verdict="accept", reason="ok"), contract)
    assert Spy.seen == [new]
    snap = trustee.snapshot()
    again = Trustee.from_snapshot(snap, mandates=MandateEngine(), paypal=PayPalClient(dry_run=True))
    assert again.reauth_count == 1 and again.reauthorized_at == trustee.reauthorized_at


# ------------------------------------------------------------------ the arena scenario


def test_partial_delivery_scenario_settles_pro_rata_in_dry_run():
    from trustee.arena.rows import to_row
    from trustee.arena.runner import DealRunner
    from trustee.arena.scenario import get_scenario

    ledger = Ledger()
    rows: list[dict] = []
    runner = DealRunner(ledger=ledger, paypal=PayPalClient(dry_run=True))
    result = runner.run(
        get_scenario("partial"), mode="scripted", on_event=lambda ev: rows.append(to_row(ev, len(rows)))
    )
    assert result.state == "SETTLED" and result.error is None
    assert result.price == "156.00"
    assert result.settlement == {
        "event": "partial_settlement",
        "basis": "units",
        "units_valid": 4500,
        "units_total": 5000,
        "captured": "140.40",
        "released": "15.60",
    }
    assert result.attest["evidence"]["units_valid"] == "4500"
    assert result.contract["partial"].startswith("units")
    tape = [r for r in rows if r["kind"] == "settlement"]
    assert tape and "settled 90%" in tape[0]["note"] and tape[0]["price"] == "140.40"
    assert result.snapshot()["settlement"]["captured"] == "140.40"


def test_other_scenarios_do_not_ship_unit_counts():
    from trustee.arena.runner import DealRunner
    from trustee.arena.scenario import get_scenario

    runner = DealRunner(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
    result = runner.run(get_scenario("cheat"), mode="scripted")
    assert result.state == "VOIDED" and "settlement" not in result.snapshot()
    assert "units_valid" not in (result.attest or {}).get("evidence", {})


def test_count_returns_units(tmp_path):
    from trustee.arena.runner import count_returns_units

    f = tmp_path / "x.csv"
    f.write_text(
        "order_id,sku,category,order_date,return_date,reason_code,refund_usd\n"
        "A1,s,c,2026-08-01,2026-08-02,R,1.00\n"
        "A1,s,c,2026-08-01,2026-08-02,R,1.00\n"  # duplicate id
        "A2,s,c,2026-08-01,2026-08-02,R,\n"  # blank refund
        "A3,s,c,2026-08-05,2026-08-02,R,2.00\n"  # returned before ordered
        "A4,s,c,2026-08-01,2026-08-02,R,2.00\n"
    )
    assert count_returns_units(f, "g") == (2, 5)
    assert count_returns_units(tmp_path / "missing.csv", "g") == (0, 0)
    f.write_text("a,b\n1,2\n")
    assert count_returns_units(f, "g") == (0, 1)


def test_partial_scenario_is_listed_only_when_enabled(monkeypatch):
    from fastapi.testclient import TestClient

    from trustee.arena.api import create_app

    def names() -> list[str]:
        app = create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
        return [s["name"] for s in TestClient(app).get("/api/scenarios").json()["scenarios"]]

    monkeypatch.delenv("ARENA_PARTIAL_SCENARIO", raising=False)
    assert "partial" not in names()
    monkeypatch.setenv("ARENA_PARTIAL_SCENARIO", "1")
    assert names()[-1] == "a2a" and "partial" in names()
