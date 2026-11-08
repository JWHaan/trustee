"""PayPal adapter tests — the endpoint shapes, not the network.

These exist because the adapter had two bugs that only show up against real
sandbox: capturing an AUTHORIZE-intent order through the orders endpoint, and
sending `payer` inside `purchase_units`. Both are invisible in dry-run mode, so
they get asserted on the request the client actually builds.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from trustee.paypal import EscrowOrder, PayPalClient


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith("/v2/checkout/orders") and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "ORDER123",
                    "status": "PAYER_ACTION_REQUIRED",
                    "links": [
                        {"rel": "payer-action", "href": "https://sandbox.paypal.com/checkoutnow?token=ORDER123"},
                    ],
                },
            )
        if path.endswith("/authorize"):
            return httpx.Response(201, json={"authorizations": [{"id": "AUTH456", "status": "CREATED"}]})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "CAP789", "status": "COMPLETED"})
        if path.endswith("/void"):
            return httpx.Response(204)
        return httpx.Response(404, json={"error": path})


def live_client(recorder: Recorder, **kwargs) -> PayPalClient:
    client = PayPalClient(client_id="id", client_secret="secret", dry_run=False, **kwargs)
    client._http = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    return client


def test_missing_credentials_means_dry_run_not_a_401():
    """The plan promised 'no creds => dry run'. `""` is falsy but not False."""
    client = PayPalClient()
    assert client.dry_run is True
    order = client.create_and_authorize(
        amount=Decimal("10.00"), currency="USD", buyer_email="b@x.dev", seller_email="s@x.dev", memo="m"
    )
    assert order.order_id.startswith("dry-order-")


def test_order_body_has_no_payer_field_and_asks_for_approval():
    rec = Recorder()
    client = live_client(rec)
    order = client.create_and_authorize(
        amount=Decimal("156.00"),
        currency="USD",
        buyer_email="b@x.dev",
        seller_email="s@x.dev",
        memo="escrow",
        custom_id="trustee:" + "c" * 64,
    )

    create = next(r for r in rec.requests if r.url.path.endswith("/v2/checkout/orders"))
    body = json.loads(create.content)
    assert body["purchase_units"][0]["custom_id"] == "trustee:" + "c" * 64  # the order names its contract

    assert body["intent"] == "AUTHORIZE"
    assert "payer" not in body["purchase_units"][0], "payer is not an Orders v2 purchase_unit field"
    assert body["purchase_units"][0]["payee"]["email_address"] == "s@x.dev"
    # Approval flow: the payer must approve before funds can be held.
    assert body["payment_source"]["paypal"]["experience_context"]["user_action"] == "PAY_NOW"
    assert order.status == "created"
    assert order.approve_url and "token=ORDER123" in order.approve_url


def test_authorize_after_approval_holds_funds():
    rec = Recorder()
    client = live_client(rec)
    order = client.authorize_after_approval("ORDER123")
    assert order.status == "authorized"
    assert order.authorization_id == "AUTH456"
    assert any(r.url.path.endswith("/v2/checkout/orders/ORDER123/authorize") for r in rec.requests)


def test_capture_goes_to_the_authorization_not_the_order():
    """The bug: /v2/checkout/orders/{id}/capture is CAPTURE-intent only."""
    rec = Recorder()
    client = live_client(rec)
    order = client.capture(EscrowOrder(order_id="ORDER123", authorization_id="AUTH456", status="authorized"))

    assert order.status == "captured"
    paths = [r.url.path for r in rec.requests]
    assert "/v2/payments/authorizations/AUTH456/capture" in paths
    assert not any("/checkout/orders/ORDER123/capture" in p for p in paths)


def test_void_goes_to_the_authorization_without_a_body():
    rec = Recorder()
    client = live_client(rec)
    order = client.void(EscrowOrder(order_id="ORDER123", authorization_id="AUTH456", status="authorized"), "reject")

    assert order.status == "voided"
    assert order.details["void_reason"] == "reject"
    void_req = next(r for r in rec.requests if r.url.path.endswith("/void"))
    assert void_req.content in (b"", b"null"), "the void endpoint takes no body"


def test_capture_without_an_authorization_is_an_explicit_error():
    client = live_client(Recorder())
    with pytest.raises(Exception) as exc:
        client.capture(EscrowOrder(order_id="ORDER123", authorization_id=None))
    assert "authorization_id" in str(exc.value)


def test_a_deal_order_names_its_signed_contract():
    """The PayPal order carries the contract's sha256 and the receipt carries the PayPal order."""
    from test_trustee_flow import make_contract

    from trustee.agents.trustee import contract_link

    contract, trustee, _ = make_contract()
    seen: dict = {}
    real = trustee.paypal.create_and_authorize

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    trustee.paypal.create_and_authorize = spy  # type: ignore[method-assign]
    trustee.on_contract(contract)
    assert seen["custom_id"] == contract_link(contract) == f"trustee:{contract.digest()}"
    assert len(seen["custom_id"]) <= 127
    assert trustee.snapshot()["escrow"]["details"]["custom_id"] == contract_link(contract)
