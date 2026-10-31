"""Money-path reliability: idempotency, retries, token expiry, failure recovery, deadlines, disputes."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from trustee.agents.arbiter import ArbiterAgent
from trustee.agents.trustee import Trustee
from trustee.ledger import Ledger
from trustee.mandate import MandateEngine, demo_mandates
from trustee.paypal import EscrowOrder, PayPalClient, PayPalError
from trustee.protocol import Accept, Attest, Contract, Delivery, GoodSpec
from trustee.state import Deal, DealState, IllegalTransition
from trustee.sweeper import DeadlineSweeper

GOOD = GoodSpec(
    good_id="g1",
    description="module",
    category="code",
    acceptance_criteria=["sha256 in manifest", "tests pass"],
    deliverable_ref="goods/m.zip",
)
T0 = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


class Fake:
    """Programmable PayPal sandbox behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, list] = {}  # path suffix -> queue of outcomes (Response | Exception)
        self.tokens = 0
        self.expires_in = 3600
        self.reject_token: str | None = None  # bearer value answered with 401 once

    def calls(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/v1/oauth2/token"):
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": self.expires_in})
        if self.reject_token and request.headers.get("authorization") == f"Bearer {self.reject_token}":
            self.reject_token = None
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        for suffix, queue in self.fail.items():
            if path.endswith(suffix) and queue:
                out = queue.pop(0)
                if isinstance(out, Exception):
                    raise out
                return out
        if path.endswith("/v2/checkout/orders"):
            return httpx.Response(201, json={"id": "ORDER1", "status": "CREATED"})
        if path.endswith("/authorize"):
            return httpx.Response(201, json={"authorizations": [{"id": "AUTH1"}]})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "CAP1"})
        if path.endswith("/void"):
            return httpx.Response(204)
        return httpx.Response(404)


def live(fake: Fake) -> PayPalClient:
    c = PayPalClient(client_id="i", client_secret="s", dry_run=False, requires_approval=False)
    c._http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return c


def amounts(**kw):
    return dict(amount=Decimal("5.00"), currency="USD", buyer_email="b@x", seller_email="s@x", memo="m", **kw)


# ----------------------------------------------------------------- paypal.py


def test_same_request_id_across_retries():
    fake = Fake()
    fake.fail["/capture"] = [httpx.Response(503), httpx.Response(503)]
    client = live(fake)
    client.capture(EscrowOrder(order_id="O", authorization_id="A", status="authorized"))
    caps = fake.calls("/capture")
    assert len(caps) == 3
    assert len({r.headers["paypal-request-id"] for r in caps}) == 1


def test_generated_request_id_is_per_call_and_explicit_id_is_sent():
    fake = Fake()
    client = live(fake)
    client.capture(EscrowOrder(order_id="O", authorization_id="A", status="authorized"), request_id="trustee-d-capture")
    client.void(EscrowOrder(order_id="O", authorization_id="A", status="authorized"))
    client.void(EscrowOrder(order_id="O", authorization_id="A", status="authorized"))
    assert fake.calls("/capture")[0].headers["paypal-request-id"] == "trustee-d-capture"
    v = [r.headers["paypal-request-id"] for r in fake.calls("/void")]
    assert len(set(v)) == 2


def test_transport_error_retried_then_unknown_outcome_error():
    fake = Fake()
    fake.fail["/capture"] = [httpx.ReadTimeout("t")] * 4
    client = live(fake)
    with pytest.raises(PayPalError) as ei:
        client.capture(EscrowOrder(order_id="O", authorization_id="A", status="authorized"))
    assert ei.value.status_code is None
    assert "unknown" in str(ei.value) and "same request id" in str(ei.value)
    assert len(fake.calls("/capture")) == 4
    assert len({r.headers["paypal-request-id"] for r in fake.calls("/capture")}) == 1


def test_transport_error_then_success():
    fake = Fake()
    fake.fail["/orders"] = [httpx.ConnectError("x")]
    order = live(fake).create_and_authorize(**amounts(request_id="rid-1"))
    assert order.status == "authorized"
    assert {r.headers["paypal-request-id"] for r in fake.calls("/orders")} == {"rid-1"}


def test_token_endpoint_transport_error_is_paypal_error():
    def boom(request):
        raise httpx.ConnectError("down")

    c = PayPalClient(client_id="i", client_secret="s", dry_run=False)
    c._http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(PayPalError):
        c.capture(EscrowOrder(order_id="O", authorization_id="A", status="authorized"))


def test_401_refreshes_token_once_and_retries():
    fake = Fake()
    client = live(fake)
    client._get_token()
    fake.reject_token = "tok1"
    client.capture(EscrowOrder(order_id="O", authorization_id="A", status="authorized"))
    assert fake.tokens == 2
    assert fake.calls("/capture")[-1].headers["authorization"] == "Bearer tok2"


def test_persistent_401_gives_up_after_one_refresh():
    fake = Fake()
    fake.fail["/capture"] = [httpx.Response(401), httpx.Response(401)]
    client = live(fake)
    resp = client._request_with_retry("POST", f"{client.base_url}/v2/payments/authorizations/A/capture")
    assert resp.status_code == 401
    assert fake.tokens == 2


def test_expired_token_is_refreshed():
    fake = Fake()
    client = live(fake)
    client._get_token()
    client._get_token()
    assert fake.tokens == 1
    client._token_expiry = time.monotonic() + 30  # inside the 60s margin
    client._get_token()
    assert fake.tokens == 2


# ------------------------------------------------------------- trustee flows


def make(fake: Fake | None = None, deadline_in: timedelta = timedelta(hours=1), judge=None, **kw):
    fake = fake or Fake()
    accept = Accept(
        sender="buyer-agent-1", responds_to="c", good=GOOD, price=Decimal("5.00"), deadline=T0 + deadline_in, ts=T0
    )
    contract = Contract.from_accept(accept, seller="seller-agent-1")
    deal = Deal(contract.deal_id)
    deal.transition(DealState.NEGOTIATING, "start")
    ledger = Ledger()
    trustee = Trustee(deal, MandateEngine(demo_mandates()), live(fake), ledger=ledger, judge=judge, **kw)
    return fake, contract, trustee, ledger


def deliver(trustee, contract):
    return trustee.on_delivery(
        Delivery(deal_id=contract.deal_id, sender="s", deliverable_ref=GOOD.deliverable_ref, content_hash="h"),
        contract,
    )


def attest(contract, verdict="accept", evidence=None):
    return Attest(deal_id=contract.deal_id, sender="b", verdict=verdict, reason="r", evidence=evidence or {})


def escrowed(**kw):
    fake, contract, trustee, ledger = make(**kw)
    assert trustee.on_contract(contract, now=T0).action == "escrowed"
    return fake, contract, trustee, ledger


def test_trustee_uses_deterministic_request_ids():
    fake, contract, trustee, _ = escrowed()
    deliver(trustee, contract)
    trustee.on_attest(attest(contract), contract)
    assert fake.calls("/orders")[0].headers["paypal-request-id"] == f"trustee-{contract.deal_id}-authorize-create"
    assert fake.calls("/capture")[0].headers["paypal-request-id"] == f"trustee-{contract.deal_id}-capture"


def test_capture_failure_leaves_delivered_and_retry_settles_once():
    fake, contract, trustee, ledger = escrowed()
    deliver(trustee, contract)
    fake.fail["/capture"] = [httpx.Response(400, json={"name": "BAD"})]
    d = trustee.on_attest(attest(contract), contract)
    assert d.action == "error" and trustee.deal.state == DealState.DELIVERED
    paypal_errs = [e for e in ledger.tail(contract.deal_id, ("paypal",)) if "error" in e.payload]
    assert paypal_errs and paypal_errs[0].payload["op"] == "capture" and paypal_errs[0].payload["request_id"]
    d = trustee.on_attest(attest(contract), contract)
    assert d.action == "settled" and trustee.deal.state == DealState.SETTLED
    ids = {r.headers["paypal-request-id"] for r in fake.calls("/capture")}
    assert len(ids) == 1
    assert trustee.on_attest(attest(contract), contract).action == "no-op"


def test_transport_failure_on_capture_does_not_raise():
    fake, contract, trustee, _ = escrowed()
    deliver(trustee, contract)
    fake.fail["/capture"] = [httpx.ReadTimeout("t")] * 4
    assert trustee.on_attest(attest(contract), contract).action == "error"
    assert trustee.on_attest(attest(contract), contract).action == "settled"


def test_void_failure_leaves_disputed_and_retry_voids():
    judge = ArbiterAgent("arb", llm=None)
    fake, contract, trustee, _ = escrowed(judge=judge)
    deliver(trustee, contract)
    fake.fail["/void"] = [httpx.Response(400, json={})]
    bad = attest(contract, "reject", {"sha256_check": "mismatch"})
    d = trustee.on_attest(bad, contract)
    assert d.action == "error" and trustee.deal.state == DealState.DISPUTED
    d = trustee.on_attest(bad, contract)
    assert d.action == "voided" and trustee.deal.state == DealState.VOIDED
    assert len({r.headers["paypal-request-id"] for r in fake.calls("/void")}) == 1


def test_tick_retries_failed_void():
    fake, contract, trustee, _ = escrowed(judge=ArbiterAgent("arb", llm=None))
    deliver(trustee, contract)
    fake.fail["/void"] = [httpx.Response(400, json={})]
    trustee.on_attest(attest(contract, "reject", {"sha256_check": "mismatch"}), contract)
    assert trustee.tick(contract, T0 + timedelta(minutes=1)).action == "voided"


def test_tick_voids_past_deadline_only():
    fake, contract, trustee, _ = escrowed()
    assert trustee.tick(contract, T0 + timedelta(minutes=30)).action == "no-op"
    assert not fake.calls("/void")
    d = trustee.tick(contract, T0 + timedelta(hours=2))
    assert d.action == "voided" and trustee.deal.state == DealState.VOIDED
    assert len(fake.calls("/void")) == 1


def test_tick_voids_delivered_deal_past_deadline():
    _, contract, trustee, _ = escrowed()
    deliver(trustee, contract)
    assert trustee.tick(contract, T0 + timedelta(hours=2)).action == "voided"


def disputed(**kw):
    fake, contract, trustee, ledger = escrowed(judge=ArbiterAgent("arb", llm=None), **kw)
    deliver(trustee, contract)
    d = trustee.on_attest(attest(contract, "reject"), contract, now=T0 + timedelta(minutes=10))
    assert d.action == "disputed" and trustee.deal.state == DealState.DISPUTED
    assert not fake.calls("/capture") and not fake.calls("/void")
    return fake, contract, trustee, ledger


def test_upheld_dispute_records_deadline_on_ruling_event():
    _, contract, trustee, ledger = disputed()
    rulings = ledger.tail(contract.deal_id, ("ruling",))
    assert trustee.dispute_deadline == T0 + timedelta(minutes=10) + timedelta(hours=72)
    assert rulings[-1].payload["dispute_deadline"] == trustee.dispute_deadline.isoformat()


def test_tick_captures_upheld_dispute_only_after_window():
    fake, contract, trustee, _ = disputed()
    assert trustee.tick(contract, T0 + timedelta(hours=71)).action == "no-op"
    assert not fake.calls("/capture")
    d = trustee.tick(contract, T0 + timedelta(hours=73))
    assert d.action == "settled" and trustee.deal.state == DealState.SETTLED
    assert len(fake.calls("/capture")) == 1


def test_dispute_not_voided_by_contract_deadline():
    fake, contract, trustee, _ = disputed(dispute_window=timedelta(hours=100))
    assert trustee.tick(contract, T0 + timedelta(hours=5)).action == "no-op"
    assert not fake.calls("/void")


def test_resolve_dispute_release_and_settle():
    fake, contract, trustee, ledger = disputed()
    d = trustee.resolve_dispute(contract, "release", "alice", "buyer proved breach")
    assert d.action == "voided" and trustee.deal.state == DealState.VOIDED
    ev = ledger.tail(contract.deal_id, ("ruling",))[-1].payload
    assert ev["operator"] == "alice" and ev["override"] is True

    fake, contract, trustee, _ = disputed()
    d = trustee.resolve_dispute(contract, "settle", "bob", "delivery fine")
    assert d.action == "settled" and trustee.deal.state == DealState.SETTLED
    assert len(fake.calls("/capture")) == 1


def test_resolve_dispute_requires_disputed_state_and_operator():
    _, contract, trustee, _ = escrowed()
    with pytest.raises(IllegalTransition):
        trustee.resolve_dispute(contract, "release", "alice", "x")
    _, contract, trustee, _ = disputed()
    with pytest.raises(ValueError):
        trustee.resolve_dispute(contract, "release", " ", "x")


def test_override_failure_keeps_dispute_open():
    fake, contract, trustee, _ = disputed()
    fake.fail["/capture"] = [httpx.Response(400, json={})]
    assert trustee.resolve_dispute(contract, "settle", "bob", "ok").action == "error"
    assert trustee.deal.state == DealState.DISPUTED
    assert trustee.resolve_dispute(contract, "settle", "bob", "ok").action == "settled"


def test_deadline_lapse_without_escrow_makes_no_paypal_calls():
    fake, contract, trustee, _ = make()
    trustee.deal.transition(DealState.CONTRACTED, "contracted")
    d = trustee.on_deadline_lapse(contract)
    assert d.action == "voided" and trustee.deal.state == DealState.VOIDED
    assert fake.requests == []


def test_failed_authorize_then_lapse_makes_no_void_call():
    fake, contract, trustee, _ = make()
    fake.fail["/orders"] = [httpx.Response(400, json={})]
    assert trustee.on_contract(contract, now=T0).action == "error"
    assert trustee.on_deadline_lapse(contract).action == "voided"
    assert not fake.calls("/void")


def test_auth_expiry_margin_voids_before_authorization_lapses():
    fake, contract, trustee, _ = escrowed(deadline_in=timedelta(days=60))
    assert trustee.tick(contract, T0 + timedelta(days=27)).action == "no-op"
    d = trustee.tick(contract, T0 + timedelta(days=28, hours=1))
    assert d.action == "voided"
    assert fake.calls("/void")


def test_auth_expiry_captures_pending_upheld_dispute():
    fake, contract, trustee, _ = disputed(dispute_window=timedelta(days=90))
    assert trustee.tick(contract, T0 + timedelta(days=10)).action == "no-op"
    assert trustee.tick(contract, T0 + timedelta(days=28, hours=1)).action == "settled"


# ------------------------------------------------------------------- sweeper


def test_sweeper_acts_and_drops_terminal_deals():
    _, c1, t1, _ = escrowed()
    _, c2, t2, _ = escrowed(deadline_in=timedelta(days=5))
    sweeper = DeadlineSweeper()
    sweeper.register(t1, c1)
    sweeper.register(t2, c2)
    assert sweeper.tick(T0 + timedelta(minutes=5)) == []
    out = sweeper.tick(T0 + timedelta(hours=2))
    assert [(d, dec.action) for d, dec in out] == [(c1.deal_id, "voided")]
    assert len(sweeper) == 1
    assert sweeper.tick(T0 + timedelta(hours=3)) == []
    sweeper.unregister(c2.deal_id)
    assert len(sweeper) == 0


def test_sweeper_isolates_a_failing_deal():
    _, c1, t1, _ = escrowed()
    _, c2, t2, _ = escrowed()

    def boom(*a, **k):
        raise RuntimeError("x")

    t1.tick = boom  # type: ignore[method-assign]
    sweeper = DeadlineSweeper()
    sweeper.register(t1, c1)
    sweeper.register(t2, c2)
    out = sweeper.tick(T0 + timedelta(hours=2))
    assert [d for d, _ in out] == [c2.deal_id]
