"""TrusteeClient against the real app, in-process."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trustee.arena.api import create_app
from trustee.client import TrusteeClient, TrusteeError
from trustee.identity import AgentKey
from trustee.ledger import Ledger
from trustee.mandate import Mandate
from trustee.paypal import PayPalClient
from trustee.platform import DealService
from trustee.protocol import Attest, Contract, Delivery, GoodSpec
from trustee.store import Store

PAYLOAD = b"hello"
BASE = "http://testserver"


@pytest.fixture
def client() -> TrusteeClient:
    paypal = PayPalClient(dry_run=True)
    service = DealService(Store(), paypal, Ledger(), operator_token="tok")
    app = create_app(ledger=Ledger(), paypal=paypal, service=service)
    return TrusteeClient(BASE, http=TestClient(app), operator_token="tok")


def make_contract(buyer: str, seller: str, price: str = "10.00") -> Contract:
    now = datetime.now()
    return Contract(
        deal_id="deal-c1",
        buyer=buyer,
        seller=seller,
        good=GoodSpec(
            good_id="g",
            description="bytes",
            category="data",
            acceptance_criteria=["sha256 matches"],
            deliverable_ref="x.bin",
            sha256=hashlib.sha256(PAYLOAD).hexdigest(),
        ),
        price=Decimal(price),
        deadline=now + timedelta(hours=1),
        formed_at=now,
    )


def onboard(c: TrusteeClient):
    org_b, org_s, kb, ks = (AgentKey.generate() for _ in range(4))
    c.register_principal("org-b", org_b)
    c.register_principal("org-s", org_s)
    c.register_agent("buyer", kb, "org-b", org_b, ["buyer"], paypal_email="b@example.com")
    c.register_agent("seller", ks, "org-s", org_s, ["seller"])
    c.publish_mandate(Mandate(agent_id="buyer", principal="org-b", max_amount=Decimal("100"), grants="spend"), org_b)
    c.publish_mandate(Mandate(agent_id="seller", principal="org-s", max_amount=Decimal("100"), grants="receive"), org_s)
    return org_b, kb, ks


def test_full_round_trip(client: TrusteeClient):
    card = client.agent_card()
    assert {s["id"] for s in card["skills"]} >= {"open-escrow", "get-deal"}
    _, kb, ks = onboard(client)
    assert client.get_agent("buyer")["has_paypal_email"] is True

    contract = make_contract("buyer", "seller").sign("buyer", kb).sign("seller", ks)
    deal = client.open_deal(contract)
    assert deal["state"] == "ESCROWED"
    assert client.open_deal(contract)["escrow"]["order_id"] == deal["escrow"]["order_id"]

    digest = hashlib.sha256(PAYLOAD).hexdigest()
    delivery = Delivery(sender="seller", deal_id="deal-c1", deliverable_ref="x.bin", content_hash=digest).sign(ks)
    assert client.deliver(delivery)["state"] == "DELIVERED"
    attest = Attest(sender="buyer", deal_id="deal-c1", verdict="accept", reason="ok").sign(kb)
    assert client.attest(attest)["state"] == "SETTLED"
    assert client.get_deal("deal-c1")["escrow"]["status"] == "captured"
    assert client.events("deal-c1")["events"]


def test_client_refuses_unsigned_messages_locally(client: TrusteeClient):
    _, kb, _ = onboard(client)
    contract = make_contract("buyer", "seller").sign("buyer", kb)  # seller missing
    with pytest.raises(ValueError, match="seller"):
        client.open_deal(contract)
    with pytest.raises(ValueError):
        client.deliver(Delivery(sender="seller", deal_id="d", deliverable_ref="x", content_hash="h"))
    with pytest.raises(ValueError):
        client.attest(Attest(sender="buyer", deal_id="d", verdict="accept", reason=""))


def test_errors_carry_status_and_body(client: TrusteeClient):
    org_b, kb, ks = onboard(client)
    with pytest.raises(TrusteeError) as err:
        client.open_deal(make_contract("buyer", "seller", price="500.00").sign("buyer", kb).sign("seller", ks))
    assert err.value.status_code == 403
    assert err.value.body["violations"]
    with pytest.raises(TrusteeError) as missing:
        client.get_deal("nope")
    assert missing.value.status_code == 404
    client.revoke_mandate("buyer", org_b)
    with pytest.raises(TrusteeError) as revoked:
        client.open_deal(make_contract("buyer", "seller").sign("buyer", kb).sign("seller", ks))
    assert revoked.value.status_code == 403


def test_resolve_uses_the_operator_token(client: TrusteeClient):
    _, kb, ks = onboard(client)
    client.open_deal(make_contract("buyer", "seller").sign("buyer", kb).sign("seller", ks))
    client.deliver(Delivery(sender="seller", deal_id="deal-c1", deliverable_ref="x.bin", content_hash="h").sign(ks))
    reject = Attest(sender="buyer", deal_id="deal-c1", verdict="reject", reason="meh").sign(kb)
    assert client.attest(reject)["state"] == "DISPUTED"
    assert client.resolve("deal-c1", "release", "ops", "refund")["state"] == "VOIDED"
