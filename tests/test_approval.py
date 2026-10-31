"""Payer approval flow against a mocked PayPal: AUTHORIZE orders hold no funds until the payer approves."""

from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from test_platform_api import T0, World

from trustee.agents.trustee import Trustee
from trustee.mandate import MandateEngine, demo_mandates
from trustee.paypal import PayPalClient
from trustee.platform.wire import revoke_signing_bytes
from trustee.protocol import Attest, Contract, GoodSpec
from trustee.state import Deal, DealState, IllegalTransition


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


class FakePayPal:
    """Programmable PayPal REST behind httpx.MockTransport. Never touches the network."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.order_status = "CREATED"  # what GET /orders/{id} reports
        self.verify_status = "SUCCESS"
        self.fail: dict[str, list] = {}  # path suffix -> queue of outcomes (Response | Exception)
        self._orders = 0

    def calls(self, suffix: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix) and (method is None or r.method == method)]

    @property
    def money_calls(self) -> int:
        return sum(len(self.calls(s, "POST")) for s in ("/authorize", "/capture", "/void"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        for suffix, queue in self.fail.items():
            if path.endswith(suffix) and queue:
                out = queue.pop(0)
                if isinstance(out, Exception):
                    raise out
                return out
        if path.endswith("/verify-webhook-signature"):
            return httpx.Response(200, json={"verification_status": self.verify_status})
        if path.endswith("/v2/checkout/orders") and request.method == "POST":
            self._orders += 1
            oid = f"ORD-{self._orders}"
            return httpx.Response(
                201,
                json={
                    "id": oid,
                    "status": "PAYER_ACTION_REQUIRED",
                    "links": [{"rel": "payer-action", "href": f"https://sandbox.paypal.test/approve?token={oid}"}],
                },
            )
        if "/v2/checkout/orders/" in path and request.method == "GET":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "status": self.order_status})
        if path.endswith("/authorize"):
            n = path.split("/")[-2].removeprefix("ORD-")
            return httpx.Response(201, json={"authorizations": [{"id": f"AUTHZ-{n}", "status": "CREATED"}]})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "CAP-1", "status": "COMPLETED"})
        if path.endswith("/void"):
            return httpx.Response(204)
        return httpx.Response(404, json={"error": path})

    def client(self, *, requires_approval: bool = True) -> PayPalClient:
        c = PayPalClient(
            client_id="id",
            client_secret="secret",
            dry_run=False,
            requires_approval=requires_approval,
            webhook_id="WH-1",
            max_retries=1,
        )
        c._http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return c


def make_world(fake: FakePayPal, db: str | Path = ":memory:") -> World:
    return World(db, paypal=fake.client())  # type: ignore[arg-type]


def approved_world() -> tuple[World, FakePayPal]:
    fake = FakePayPal()
    w = make_world(fake).setup()
    assert w.open(w.contract()).status_code == 201
    return w, fake


def state(w: World, deal_id: str = "deal-1") -> str:
    return str(w.http.get(f"/v1/deals/{deal_id}").json()["state"])


# ------------------------------------------------------------------ flow


def test_deal_waits_for_approval_with_an_approve_url_and_holds_nothing():
    w, fake = approved_world()
    view = w.http.get("/v1/deals/deal-1").json()
    assert view["state"] == "CONTRACTED"
    assert view["escrow"]["status"] == "created"
    assert view["escrow"]["approve_url"].endswith("token=ORD-1")
    assert fake.money_calls == 0


def test_return_url_authorizes_exactly_once_even_when_repeated():
    w, fake = approved_world()
    r = w.http.get("/v1/paypal/return", params={"token": "ORD-1", "PayerID": "P"})
    assert r.status_code == 200
    assert "ESCROWED" in r.text and r.headers["content-type"].startswith("text/html")
    assert state(w) == "ESCROWED"
    assert len(fake.calls("/authorize")) == 1
    again = w.http.get("/v1/paypal/return", params={"token": "ORD-1"}, headers={"Accept": "application/json"})
    assert again.status_code == 200 and again.json()["decision"] == "no-op"
    assert len(fake.calls("/authorize")) == 1
    escrow = w.http.get("/v1/deals/deal-1").json()["escrow"]
    assert escrow["status"] == "authorized" and escrow["authorization_id"] == "AUTHZ-1"
    # The authorize call carries the deterministic request id.
    assert fake.calls("/authorize")[0].headers["paypal-request-id"] == "trustee-deal-1-authorize"


def test_approved_deal_then_settles_normally():
    w, fake = approved_world()
    w.http.get("/v1/paypal/return", params={"token": "ORD-1"})
    assert w.deliver("deal-1").status_code == 200
    assert w.attest("deal-1", evidence={"sha256_check": "match"}).json()["state"] == "SETTLED"
    assert len(fake.calls("/capture")) == 1


def test_cancel_voids_the_deal_without_calling_paypal():
    w, fake = approved_world()
    before = len(fake.requests)
    r = w.http.get("/v1/paypal/cancel", params={"token": "ORD-1"}, headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["decision"] == "voided"
    assert state(w) == "VOIDED"
    assert len(fake.requests) == before
    assert fake.money_calls == 0


def test_return_after_cancel_does_not_authorize():
    w, fake = approved_world()
    w.http.get("/v1/paypal/cancel", params={"token": "ORD-1"})
    w.http.get("/v1/paypal/return", params={"token": "ORD-1"})
    assert state(w) == "VOIDED" and fake.money_calls == 0


def test_unknown_token_is_404():
    w, _ = approved_world()
    assert w.http.get("/v1/paypal/return", params={"token": "nope"}).status_code == 404
    assert w.http.get("/v1/paypal/cancel", params={"token": "nope"}).status_code == 404


def test_approval_past_the_deadline_voids_via_sweep_without_calling_paypal():
    w, fake = approved_world()
    acted = w.service.sweep(T0 + timedelta(hours=2))
    assert ("deal-1", "voided") in acted
    assert state(w) == "VOIDED"
    assert fake.money_calls == 0


def test_sweep_polls_and_authorizes_an_approved_order():
    w, fake = approved_world()
    assert w.service.sweep(T0 + timedelta(minutes=1)) == []  # still CREATED at PayPal
    assert state(w) == "CONTRACTED" and fake.money_calls == 0
    fake.order_status = "APPROVED"
    assert ("deal-1", "escrowed") in w.service.sweep(T0 + timedelta(minutes=2))
    assert state(w) == "ESCROWED"
    w.service.sweep(T0 + timedelta(minutes=3))
    assert len(fake.calls("/authorize")) == 1


def test_sweep_polls_at_most_twenty_orders_and_survives_poll_errors():
    fake = FakePayPal()
    w = make_world(fake).setup()
    for i in range(25):
        assert w.open(w.contract(f"deal-{i}")).status_code == 201
    fake.fail["/v2/checkout/orders/ORD-1"] = [httpx.Response(500)] * 10  # one order's read keeps failing
    w.service.sweep(T0 + timedelta(minutes=1))
    polls = [r for r in fake.requests if r.method == "GET" and "/v2/checkout/orders/" in r.url.path]
    assert 0 < len(polls) <= 20 + 2  # 20 reads (+ retry of the failing one), never all 25


def test_sweep_closes_a_deal_paypal_permanently_refuses_to_authorize():
    """An invalid payee: PayPal takes the approval, then refuses the authorize with a 422. The sweep must
    try once and close the deal (nothing held), not retry every tick until PayPal locks the order."""
    w, fake = approved_world()
    fake.order_status = "APPROVED"
    refusal = httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYEE_ACCOUNT_INVALID"}]})
    fake.fail["/authorize"] = [refusal] * 5
    assert ("deal-1", "voided") in w.service.sweep(T0 + timedelta(minutes=1))
    assert state(w) == "VOIDED"
    w.service.sweep(T0 + timedelta(minutes=2))
    assert len(fake.calls("/authorize")) == 1
    assert len(fake.calls("/capture")) == 0 and len(fake.calls("/void")) == 0


def test_sweep_keeps_retrying_a_transient_authorize_failure():
    w, fake = approved_world()
    fake.order_status = "APPROVED"
    fake.fail["/authorize"] = [httpx.Response(503), httpx.Response(503)]  # max_retries=1: one call = 2 attempts
    acted = w.service.sweep(T0 + timedelta(minutes=1))
    assert ("deal-1", "error") in acted and state(w) == "CONTRACTED"
    assert ("deal-1", "escrowed") in w.service.sweep(T0 + timedelta(minutes=2))
    assert state(w) == "ESCROWED"


def test_revoked_mandate_blocks_authorization_after_approval():
    w, fake = approved_world()
    sig = w.keys["org-b"].sign(revoke_signing_bytes("buyer"))
    assert w.post("/v1/mandates/buyer/revoke", {"signature": sig}).status_code == 200
    r = w.http.get("/v1/paypal/return", params={"token": "ORD-1"}, headers={"Accept": "application/json"})
    assert r.status_code == 403 and r.json()["decision"] == "rejected_mandate"
    assert state(w) == "CONTRACTED"
    assert fake.money_calls == 0


def test_paypal_error_on_authorize_keeps_state_and_retry_works():
    w, fake = approved_world()
    fake.fail["/authorize"] = [httpx.Response(422, json={"name": "ORDER_NOT_APPROVED"})]
    r = w.http.get("/v1/paypal/return", params={"token": "ORD-1"}, headers={"Accept": "application/json"})
    assert r.status_code == 502 and r.json()["decision"] == "error"
    assert state(w) == "CONTRACTED"
    r = w.http.get("/v1/paypal/return", params={"token": "ORD-1"}, headers={"Accept": "application/json"})
    assert r.json()["decision"] == "escrowed" and state(w) == "ESCROWED"


def test_restart_while_awaiting_approval_then_approve(tmp_path: Path):
    db = tmp_path / "approval.db"
    fake = FakePayPal()
    w1 = make_world(fake, db).setup()
    assert w1.open(w1.contract()).status_code == 201
    w1.store.close()

    w2 = make_world(fake, db)
    w2.keys = w1.keys
    view = w2.http.get("/v1/deals/deal-1").json()
    assert view["state"] == "CONTRACTED" and view["escrow"]["approve_url"].endswith("token=ORD-1")
    r = w2.http.get("/v1/paypal/return", params={"token": "ORD-1"})
    assert r.status_code == 200 and state(w2) == "ESCROWED"
    assert len(fake.calls("/authorize")) == 1
    # Budget is reserved only now that funds are held.
    assert w2.service.mandates.committed("buyer") == Decimal("25.00")
    w2.store.close()


# ------------------------------------------------- trustee-level money guard


def test_attest_accept_on_an_escrowed_deal_makes_no_capture(monkeypatch):
    paypal = PayPalClient(dry_run=True)
    captures: list[object] = []
    monkeypatch.setattr(PayPalClient, "capture", lambda self, order, **kw: captures.append(order))
    contract = Contract(
        deal_id="d1",
        buyer="buyer-agent-1",
        seller="seller-agent-1",
        good=GoodSpec(
            good_id="g",
            description="m",
            category="code",
            acceptance_criteria=["x"],
            deliverable_ref="m.zip",
        ),
        price=Decimal("10.00"),
        deadline=T0 + timedelta(hours=1),
        formed_at=T0,
    )
    deal = Deal("d1")
    deal.transition(DealState.NEGOTIATING)
    trustee = Trustee(deal, MandateEngine(demo_mandates()), paypal)
    assert trustee.on_contract(contract, T0).action == "escrowed"
    with pytest.raises(IllegalTransition):
        trustee.on_attest(Attest(sender="buyer-agent-1", deal_id="d1", verdict="accept", reason="ok"), contract)
    assert captures == []
    assert deal.state == DealState.ESCROWED
