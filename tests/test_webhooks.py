"""PayPal webhooks as ground truth: verification, dedupe, reconciliation of unknown outcomes, mandate versions."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import httpx
import pytest
from test_approval import FakePayPal
from test_platform_api import World

from trustee.mandate import Mandate, MandateEngine, MandateReplay
from trustee.paypal import PayPalClient
from trustee.platform.wire import mandate_to_dict

PAYPAL_HEADERS = {
    "PayPal-Auth-Algo": "SHA256withRSA",
    "PayPal-Cert-Url": "https://api.sandbox.paypal.test/cert.pem",
    "PayPal-Transmission-Id": "tx-1",
    "PayPal-Transmission-Sig": "sig",
    "PayPal-Transmission-Time": "2026-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def event(event_id: str, event_type: str, resource: dict) -> dict:
    return {"id": event_id, "event_type": event_type, "resource": resource}


def capture_event(event_id: str, event_type: str = "PAYMENT.CAPTURE.COMPLETED", authz: str = "AUTHZ-1") -> dict:
    return event(
        event_id,
        event_type,
        {"id": "CAP-1", "supplementary_data": {"related_ids": {"authorization_id": authz, "order_id": "ORD-1"}}},
    )


def send(w: World, body: dict):
    return w.http.post("/v1/paypal/webhook", json=body, headers=PAYPAL_HEADERS)


def escrowed_world() -> tuple[World, FakePayPal]:
    """Funds already held (single-shot authorize), order ORD-1 / authorization AUTHZ-1."""
    fake = FakePayPal()
    w = World(":memory:", paypal=fake.client(requires_approval=False)).setup()  # type: ignore[arg-type]
    assert w.open(w.contract()).json()["state"] == "ESCROWED"
    return w, fake


def view(w: World) -> dict:
    return w.http.get("/v1/deals/deal-1").json()


def kinds(w: World) -> list[str]:
    return [e["kind"] for e in w.http.get("/v1/deals/deal-1/events").json()["events"]]


# ----------------------------------------------------------- verification


def test_bad_signature_is_400_and_records_nothing():
    w, fake = escrowed_world()
    fake.verify_status = "FAILURE"
    r = send(w, capture_event("evt-1"))
    assert r.status_code == 400
    fake.verify_status = "SUCCESS"
    assert w.store.record_webhook("evt-1", "x", None, {}) is True  # it was never recorded
    assert view(w)["state"] == "ESCROWED"


def test_dry_run_rejects_webhooks_unless_explicitly_allowed():
    assert PayPalClient(dry_run=True).verify_webhook(PAYPAL_HEADERS, {}) is False
    assert PayPalClient(dry_run=True, allow_unverified_webhooks=True).verify_webhook(PAYPAL_HEADERS, {}) is True


def test_verify_webhook_request_shape_and_result():
    fake = FakePayPal()
    client = fake.client()
    body = event("evt-1", "CHECKOUT.ORDER.APPROVED", {"id": "ORD-1"})
    lowercase = {k.lower(): v for k, v in PAYPAL_HEADERS.items()}
    assert client.verify_webhook(lowercase, body) is True
    req = fake.calls("/v1/notifications/verify-webhook-signature")[0]
    sent = json.loads(req.content)
    assert req.method == "POST"
    assert sent == {
        "auth_algo": "SHA256withRSA",
        "cert_url": "https://api.sandbox.paypal.test/cert.pem",
        "transmission_id": "tx-1",
        "transmission_sig": "sig",
        "transmission_time": "2026-01-01T00:00:00Z",
        "webhook_id": "WH-1",
        "webhook_event": body,
    }
    fake.verify_status = "FAILURE"
    assert client.verify_webhook(PAYPAL_HEADERS, body) is False  # mixed-case headers work too


def test_verify_webhook_fails_closed():
    fake = FakePayPal()
    client = fake.client()
    assert client.verify_webhook({}, {}) is False  # missing transmission headers: no call made
    assert fake.calls("/verify-webhook-signature") == []
    client.webhook_id = None
    assert client.verify_webhook(PAYPAL_HEADERS, {}) is False  # live with no webhook id
    client.webhook_id = "WH-1"
    fake.fail["/verify-webhook-signature"] = [httpx.Response(500)] * 5
    assert client.verify_webhook(PAYPAL_HEADERS, {}) is False  # PayPal outage


def test_get_order_dry_run_and_live():
    assert PayPalClient(dry_run=True).get_order("O1") == {"id": "O1", "status": "APPROVED"}
    fake = FakePayPal()
    fake.order_status = "APPROVED"
    assert fake.client().get_order("ORD-9")["status"] == "APPROVED"


# ------------------------------------------------------------ event types


def test_order_approved_webhook_authorizes_once_and_duplicates_change_nothing():
    fake = FakePayPal()
    w = World(":memory:", paypal=fake.client()).setup()  # type: ignore[arg-type]
    w.open(w.contract())
    body = event("evt-1", "CHECKOUT.ORDER.APPROVED", {"id": "ORD-1"})
    r = send(w, body)
    assert r.status_code == 200 and r.json()["handled"] == "escrowed"
    assert view(w)["state"] == "ESCROWED"
    dup = send(w, body)
    assert dup.status_code == 200 and dup.json() == {"duplicate": True}
    # A second, distinct event for the same order is a replay: no second authorize.
    send(w, event("evt-2", "CHECKOUT.ORDER.APPROVED", {"id": "ORD-1"}))
    assert len(fake.calls("/authorize")) == 1


def test_authorization_voided_resolves_a_void_that_timed_out():
    w, fake = escrowed_world()
    w.deliver("deal-1", content_hash="b" * 64)
    fake.fail["/void"] = [httpx.ReadTimeout("lost")] * 4
    r = w.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"})
    assert r.status_code == 502  # outcome unknown
    assert view(w)["state"] == "DISPUTED"
    assert w.service.mandates.committed("buyer") == Decimal("25.00")

    r = send(w, event("evt-1", "PAYMENT.AUTHORIZATION.VOIDED", {"id": "AUTHZ-1"}))
    assert r.status_code == 200 and r.json()["handled"] == "voided"
    v = view(w)
    assert v["state"] == "VOIDED" and v["escrow"]["status"] == "voided"
    assert w.service.mandates.committed("buyer") == 0  # budget released
    assert "reconcile" in kinds(w)
    # Another void event, or our own retry, changes nothing.
    assert send(w, event("evt-2", "PAYMENT.AUTHORIZATION.VOIDED", {"id": "AUTHZ-1"})).json()["handled"] == "no-op"


def test_authorization_voided_while_delivered_voids_the_deal():
    w, _ = escrowed_world()
    w.deliver("deal-1")
    assert send(w, event("evt-1", "PAYMENT.AUTHORIZATION.VOIDED", {"id": "AUTHZ-1"})).json()["handled"] == "voided"
    assert view(w)["state"] == "VOIDED"


def test_capture_completed_resolves_a_capture_that_timed_out():
    w, fake = escrowed_world()
    w.deliver("deal-1")
    fake.fail["/capture"] = [httpx.ReadTimeout("lost")] * 4
    r = w.attest("deal-1", evidence={"sha256_check": "match"})
    assert r.status_code == 502
    assert view(w)["state"] == "DELIVERED"

    r = send(w, capture_event("evt-1"))
    assert r.status_code == 200 and r.json()["handled"] == "settled"
    v = view(w)
    assert v["state"] == "SETTLED" and v["escrow"]["status"] == "captured"
    assert w.service.mandates.committed("buyer") == Decimal("25.00")  # now spend, not reservation
    assert "reconcile" in kinds(w)
    assert send(w, capture_event("evt-2")).json()["handled"] == "no-op"


def test_capture_completed_falls_back_to_the_order_id():
    w, _ = escrowed_world()
    w.deliver("deal-1")
    body = event(
        "evt-1",
        "PAYMENT.CAPTURE.COMPLETED",
        {"id": "CAP-1", "supplementary_data": {"related_ids": {"order_id": "ORD-1"}}},
    )
    assert send(w, body).json()["handled"] == "settled"


def test_capture_completed_on_an_escrowed_deal_flags_it_and_changes_no_state():
    w, _ = escrowed_world()
    r = send(w, capture_event("evt-1"))
    assert r.status_code == 200 and r.json()["handled"] == "alert"
    v = view(w)
    assert v["state"] == "ESCROWED" and v["needs_attention"]
    assert "paypal_alert" in kinds(w)


def test_capture_completed_on_a_voided_deal_flags_it():
    w, _ = escrowed_world()
    w.deliver("deal-1", content_hash="b" * 64)
    assert w.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"}).json()["state"] == "VOIDED"
    assert send(w, capture_event("evt-1")).json()["handled"] == "alert"
    v = view(w)
    assert v["state"] == "VOIDED" and "VOIDED" in v["needs_attention"]


@pytest.mark.parametrize("etype", ["PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.REFUNDED"])
def test_capture_problems_raise_an_alert_without_state_change(etype: str):
    w, _ = escrowed_world()
    w.deliver("deal-1")
    r = send(w, capture_event("evt-1", etype))
    assert r.status_code == 200 and r.json()["handled"] == "alert"
    v = view(w)
    assert v["state"] == "DELIVERED" and etype in v["needs_attention"]


def test_needs_attention_survives_a_restart(tmp_path):
    db = tmp_path / "wh.db"
    fake = FakePayPal()
    w1 = World(db, paypal=fake.client(requires_approval=False)).setup()  # type: ignore[arg-type]
    w1.open(w1.contract())
    send(w1, capture_event("evt-1"))
    w1.store.close()
    w2 = World(db, paypal=fake.client(requires_approval=False))  # type: ignore[arg-type]
    assert view(w2)["needs_attention"]
    w2.store.close()


def test_unknown_event_type_and_unknown_resource_are_ignored_with_200():
    w, _ = escrowed_world()
    r = send(w, event("evt-1", "BILLING.PLAN.CREATED", {"id": "X"}))
    assert r.status_code == 200 and r.json() == {"ignored": "BILLING.PLAN.CREATED"}
    r = send(w, event("evt-2", "PAYMENT.AUTHORIZATION.VOIDED", {"id": "AUTHZ-404"}))
    assert r.status_code == 200 and r.json() == {"ignored": "unknown resource"}
    assert view(w)["state"] == "ESCROWED"


def test_webhook_for_a_deal_from_before_a_restart_still_resolves(tmp_path):
    db = tmp_path / "old.db"
    fake = FakePayPal()
    w1 = World(db, paypal=fake.client(requires_approval=False)).setup()  # type: ignore[arg-type]
    w1.open(w1.contract())
    w1.deliver("deal-1", content_hash="b" * 64)
    assert w1.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"}).json()["state"] == "VOIDED"
    w1.store.close()
    w2 = World(db, paypal=fake.client(requires_approval=False))  # type: ignore[arg-type]
    w2.keys = w1.keys
    assert send(w2, capture_event("evt-1")).json()["handled"] == "alert"  # terminal deal found via the store scan
    w2.store.close()


def test_non_json_webhook_body_is_400():
    w, _ = escrowed_world()
    assert w.http.post("/v1/paypal/webhook", content=b"not json", headers=PAYPAL_HEADERS).status_code == 400


# ------------------------------------------------------- mandate versions


def _mandate(version: int, cap: str = "500.00") -> Mandate:
    return Mandate(agent_id="a", principal="p", max_amount=Decimal(cap), version=version)


def test_engine_refuses_a_stale_or_replayed_mandate_version():
    engine = MandateEngine()
    engine.grant(_mandate(0))
    engine.grant(_mandate(0))  # identical body: idempotent
    with pytest.raises(MandateReplay):
        engine.grant(_mandate(0, cap="999.00"))  # different body at the same version
    engine.grant(_mandate(1, cap="999.00"))
    with pytest.raises(MandateReplay):
        engine.grant(_mandate(0))  # replay of the older signed mandate
    mandate = engine.get("a")
    assert mandate is not None and mandate.version == 1


def test_version_is_part_of_the_signed_bytes():
    assert _mandate(0).signing_bytes() != _mandate(1).signing_bytes()


def test_publishing_an_older_mandate_is_409():
    w = World().setup()
    k = w.keys
    base = Mandate(
        agent_id="buyer",
        principal="org-b",
        max_amount=Decimal("500.00"),
        allowed_categories=frozenset({"code"}),
        allowed_counterparties=frozenset({"seller"}),
        grants="spend",
    )
    v1 = Mandate(**{**base.__dict__, "version": 1, "max_amount": Decimal("100.00")})
    assert w.post("/v1/mandates", mandate_to_dict(v1.sign(k["org-b"]))).status_code == 201
    replay = w.post("/v1/mandates", mandate_to_dict(base.sign(k["org-b"])))  # the version-0 mandate from setup()
    assert replay.status_code == 409
    again = w.post("/v1/mandates", mandate_to_dict(v1.sign(k["org-b"])))  # identical re-publish is fine
    assert again.status_code == 201
    assert w.service.mandates.get("buyer").max_amount == Decimal("100.00")  # type: ignore[union-attr]
