"""Accountability under failure: the money intent is written before PayPal is called, a crash between the
PayPal call and the write-back is finished with the same request id, storage failure blocks money instead of
being ignored, and a buyer's hash-mismatch claim that the signed record contradicts does not refund them."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_platform_api import GOOD_HASH, CountingPayPal, World

from trustee.alerts import classify
from trustee.ledger import verify_chain
from trustee.paypal import EscrowOrder
from trustee.state import DealState


class Crash(BaseException):
    """The process dying: not an Exception, so nothing on the money path catches it."""


class IdempotentPayPal(CountingPayPal):
    """Dry-run PayPal that, like the real one, answers a repeated request id with the first result."""

    def __init__(self) -> None:
        super().__init__()
        self.orders: dict[str, EscrowOrder] = {}
        self.request_ids: list[str] = []
        self.crash_after_authorize = False
        self.before_authorize = None  # optional probe, called with the request id before the "PayPal" call

    def create_and_authorize(self, *args, request_id: str | None = None, **kwargs) -> EscrowOrder:
        if self.before_authorize is not None:
            self.before_authorize(request_id)
        self.request_ids.append(str(request_id))
        if request_id in self.orders:
            return self.orders[request_id]
        order = super().create_and_authorize(*args, request_id=request_id, **kwargs)
        self.orders[str(request_id)] = order
        if self.crash_after_authorize:
            raise Crash("process died after PayPal held the funds")
        return order


@pytest.fixture
def world() -> World:
    return World().setup()


# ------------------------------------------------------------ hash cross-check


def test_false_hash_mismatch_claim_is_held_not_refunded(world: World):
    """The contract commits to GOOD_HASH and the seller signs a delivery with GOOD_HASH. A buyer who then claims
    "mismatch" used to be refunded while keeping the file. Now the funds stay held and a human is flagged."""
    world.open(world.contract())
    world.deliver("deal-1", content_hash=GOOD_HASH)
    r = world.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "DISPUTED"
    assert body["escrow"]["status"] == "authorized"  # still held: neither refunded nor paid out
    assert world.paypal.calls["void"] == 0 and world.paypal.calls["capture"] == 0
    trustee, _ = world.service._require("deal-1")
    assert trustee.needs_attention and "contradicts the signed record" in trustee.needs_attention
    rulings = [e for e in world.http.get("/v1/deals/deal-1/events").json()["events"] if e["kind"] == "ruling"]
    assert rulings[-1]["payload"]["decision"] == "uphold"


def test_true_hash_mismatch_still_refunds(world: World):
    """When the seller's own signed hash differs from the contract, the buyer's claim is corroborated: refund."""
    world.open(world.contract())
    world.deliver("deal-1", content_hash="b" * 64)
    r = world.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"})
    assert r.json()["state"] == "VOIDED"
    assert world.paypal.calls["void"] == 1


# ------------------------------------------------------- write before money


def test_intent_is_on_disk_before_paypal_is_called(tmp_path: Path):
    pp = IdempotentPayPal()
    w = World(tmp_path / "t.db", paypal=pp).setup()
    seen: list[dict] = []

    def probe(request_id: str | None) -> None:
        row = w.store.get_deal("deal-1")
        assert row is not None, "PayPal was called before the deal was written"
        seen.append(row["snapshot"]["pending_money"])
        assert row["snapshot"]["pending_money"]["request_id"] == request_id

    pp.before_authorize = probe
    assert w.open(w.contract()).json()["state"] == "ESCROWED"
    assert seen and seen[0]["op"] == "authorize-create"
    # Once the result is written, nothing is pending any more.
    assert "pending_money" not in w.store.get_deal("deal-1")["snapshot"]


def test_crash_after_paypal_authorizes_is_finished_by_the_next_sweep(tmp_path: Path):
    """Before: the hold existed at PayPal and no deal row did, so nothing on the server could find it."""
    db = tmp_path / "t.db"
    pp = IdempotentPayPal()
    w1 = World(db, paypal=pp).setup(max_total="30.00")
    pp.crash_after_authorize = True
    with pytest.raises(Crash):
        w1.service.open_deal(w1.contract())
    assert len(pp.orders) == 1  # PayPal holds the funds
    w1.store.close()

    pp.crash_after_authorize = False
    w2 = World(db, paypal=pp)  # restart
    w2.keys = w1.keys
    row = w2.store.get_deal("deal-1")
    assert row is not None and row["state"] == DealState.CONTRACTED.value  # the write-ahead survived the crash
    assert w2.service.sweep() == [("deal-1", "escrowed")]
    view = w2.http.get("/v1/deals/deal-1").json()
    assert view["state"] == "ESCROWED" and view["escrow"]["status"] == "authorized"
    # The resume re-sent the same request id, so PayPal returned the first order: one hold, not two.
    assert len(pp.orders) == 1 and len(set(pp.request_ids)) == 1 and len(pp.request_ids) == 2
    # The budget reservation is back too: a second 25 USD deal would exceed the 30 USD total.
    assert w2.open(w2.contract("deal-2")).status_code == 403
    assert w2.deliver("deal-1").json()["state"] == "DELIVERED"
    assert w2.attest("deal-1", evidence={"sha256_check": "match"}).json()["state"] == "SETTLED"
    assert pp.calls["capture"] == 1


def test_client_retry_after_crash_resumes_instead_of_deciding_again(tmp_path: Path):
    db = tmp_path / "t.db"
    pp = IdempotentPayPal()
    w1 = World(db, paypal=pp).setup()
    pp.crash_after_authorize = True
    with pytest.raises(Crash):
        w1.service.open_deal(w1.contract())
    w1.store.close()

    pp.crash_after_authorize = False
    w2 = World(db, paypal=pp)
    w2.keys = w1.keys
    r = w2.open(w2.contract())
    assert r.status_code == 200 and r.json()["state"] == "ESCROWED"
    assert len(pp.orders) == 1


# ------------------------------------------------------------- fail closed


def test_no_paypal_call_when_the_intent_cannot_be_written(world: World, monkeypatch: pytest.MonkeyPatch):
    real = world.store.upsert_deal

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(world.store, "upsert_deal", broken)
    r = world.open(world.contract())
    assert r.status_code == 503
    assert world.paypal.total == 0  # nothing was sent to PayPal
    assert world.service.durability()["deals_waiting"] == 1
    notes = [
        e
        for e in world.service.ledger.tail("deal-1")
        if e.kind == "note" and e.payload.get("op") == "intent_not_recorded"
    ]
    assert notes and classify(notes[-1])[0] == "storage_unavailable"  # a person is told, with the right reason

    monkeypatch.setattr(world.store, "upsert_deal", real)
    r = world.open(world.contract())  # the retry
    assert r.status_code == 201 or r.status_code == 200
    assert r.json()["state"] == "ESCROWED"
    assert world.paypal.calls["authorize"] == 1
    world.service.sweep()
    assert world.service.durability() == {"events_waiting": 0, "deals_waiting": 0}


def test_unwritten_ledger_events_block_money_and_are_kept_in_order(world: World, monkeypatch: pytest.MonkeyPatch):
    real = world.store.append_event

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(world.store, "append_event", broken)
    r = world.open(world.contract())
    assert r.status_code == 503
    assert world.paypal.total == 0
    health = world.http.get("/healthz").json()
    assert health["durability"]["events_waiting"] > 0

    monkeypatch.setattr(world.store, "append_event", real)
    world.service.sweep()  # writes the backlog
    assert world.service.durability()["events_waiting"] == 0
    assert world.open(world.contract()).json()["state"] == "ESCROWED"
    events = world.store.events("deal-1")
    assert verify_chain(events) == (True, "ok")  # the late events landed in order: the chain is intact
