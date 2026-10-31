"""End-to-end Trustee flows: honest deal settles, cheat deal refunds."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from trustee.agents.trustee import Trustee
from trustee.identity import AgentKey, AgentRecord, AgentRegistry, PrincipalRegistry
from trustee.ledger import Ledger
from trustee.mandate import MandateEngine, demo_mandates
from trustee.paypal import PayPalClient
from trustee.protocol import Accept, Attest, Contract, Delivery, GoodSpec
from trustee.state import Deal, DealState

GOOD = GoodSpec(
    good_id="lambda-module-1",
    description="Lambdas math module, tested",
    category="code",
    acceptance_criteria=["sha256 in manifest", "pytest suite passes"],
    deliverable_ref="goods/lambda-module.zip",
)


def make_contract(price: str = "150.00") -> tuple[Contract, Trustee, Ledger]:
    accept = Accept(
        sender="buyer-agent-1",
        responds_to="counter-1",
        good=GOOD,
        price=Decimal(price),
        deadline=datetime.now() + timedelta(hours=1),
    )
    contract = Contract.from_accept(accept, seller="seller-agent-1")
    deal = Deal(contract.deal_id)
    deal.transition(DealState.NEGOTIATING, "negotiation started")
    ledger = Ledger()
    mandates = MandateEngine(demo_mandates())
    paypal = PayPalClient(dry_run=True)
    trustee = Trustee(deal, mandates, paypal, ledger=ledger)
    return contract, trustee, ledger


def test_honest_deal_settles():
    contract, trustee, ledger = make_contract()
    d = trustee.on_contract(contract)
    assert d.action == "escrowed" and trustee.deal.state == DealState.ESCROWED

    delivery = Delivery(
        deal_id=contract.deal_id, sender="seller-agent-1", deliverable_ref=GOOD.deliverable_ref, content_hash="ab12cd"
    )
    assert trustee.on_delivery(delivery, contract).action == "delivered"
    assert trustee.deal.state == DealState.DELIVERED

    att = Attest(deal_id=contract.deal_id, sender="buyer-agent-1", verdict="accept", reason="hash matches, tests pass")
    d = trustee.on_attest(att, contract)
    assert d.action == "settled" and trustee.deal.state == DealState.SETTLED
    assert d.escrow is not None and d.escrow.status == "captured"

    kinds = [e.kind for e in ledger.tail(contract.deal_id)]
    assert "mandate" in kinds and "paypal" in kinds and "delivery" in kinds


def test_cheat_deal_voids_and_refunds():
    contract, trustee, _ = make_contract()
    trustee.on_contract(contract)
    delivery = Delivery(
        deal_id=contract.deal_id, sender="seller-agent-1", deliverable_ref=GOOD.deliverable_ref, content_hash="wrong"
    )
    trustee.on_delivery(delivery, contract)

    att = Attest(
        deal_id=contract.deal_id,
        sender="buyer-agent-1",
        verdict="reject",
        reason="sha256 does not match contract criterion 'sha256 in manifest'",
        evidence={"sha256_check": "mismatch", "clause": "acceptance_criteria[0]"},
    )
    d = trustee.on_attest(att, contract)
    assert d.action == "voided" and trustee.deal.state == DealState.VOIDED
    assert d.escrow is not None and d.escrow.status == "voided"


def test_mandate_gate_blocks_oversized_deal():
    contract, trustee, _ = make_contract(price="501.00")
    d = trustee.on_contract(contract)
    assert d.action == "rejected_mandate"
    assert d.report is not None and not d.report.ok
    assert trustee.deal.state == DealState.NEGOTIATING  # money never opened


def test_deadline_lapse_voids():
    contract, trustee, _ = make_contract()
    trustee.on_contract(contract)
    d = trustee.on_deadline_lapse(contract)
    assert d.action == "voided" and trustee.deal.state == DealState.VOIDED


def test_terminal_deals_are_immutable():
    contract, trustee, _ = make_contract()
    trustee.on_contract(contract)
    delivery = Delivery(
        deal_id=contract.deal_id, sender="seller-agent-1", deliverable_ref=GOOD.deliverable_ref, content_hash="ok"
    )
    trustee.on_delivery(delivery, contract)
    att = Attest(deal_id=contract.deal_id, sender="buyer-agent-1", verdict="accept", reason="ok")
    assert trustee.on_attest(att, contract).action == "settled"
    # duplicate webhook delivery must be an idempotent no-op, not a double-settle
    d = trustee.on_attest(att, contract)
    assert d.action == "no-op" and trustee.deal.state == DealState.SETTLED


# ------------------------------------------------- signed identity, budgets, snapshots


class CountingPayPal(PayPalClient):
    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.ops: list[str] = []

    def create_and_authorize(self, **kwargs):  # type: ignore[override]
        self.ops.append("authorize")
        return super().create_and_authorize(**kwargs)

    def capture(self, order, *, request_id=None):  # type: ignore[override]
        self.ops.append("capture")
        return super().capture(order, request_id=request_id)

    def void(self, order, reason="", *, request_id=None):  # type: ignore[override]
        self.ops.append("void")
        return super().void(order, reason=reason, request_id=request_id)


class Signed:
    """A deal wired with keys, a registry, principal-signed mandates and a counting PayPal."""

    def __init__(self, price: str = "150.00", sign_seller: bool = True, **trustee_kw):
        self.bk, self.sk, self.pk = AgentKey.generate(), AgentKey.generate(), AgentKey.generate()
        self.registry = AgentRegistry()
        self.registry.register(
            AgentRecord("buyer-agent-1", self.bk.public_key_b64, "demo-principal", frozenset({"buyer"}))
        )
        self.registry.register(
            AgentRecord(
                "seller-agent-1",
                self.sk.public_key_b64,
                "demo-principal",
                frozenset({"seller"}),
                paypal_email="registered-seller@example.com",
            )
        )
        principals = PrincipalRegistry()
        principals.register("demo-principal", self.pk.public_key_b64)
        self.mandates = MandateEngine({a: m.sign(self.pk) for a, m in demo_mandates().items()}, principals=principals)
        self.paypal = CountingPayPal()
        self.ledger = Ledger()
        accept = Accept(
            sender="buyer-agent-1",
            responds_to="c",
            good=GOOD,
            price=Decimal(price),
            deadline=datetime.now() + timedelta(hours=1),
        )
        contract = Contract.from_accept(accept, seller="seller-agent-1").sign("buyer-agent-1", self.bk)
        self.contract = contract.sign("seller-agent-1", self.sk) if sign_seller else contract
        deal = Deal(self.contract.deal_id)
        deal.transition(DealState.NEGOTIATING, "start")
        self.trustee = Trustee(
            deal, self.mandates, self.paypal, ledger=self.ledger, registry=self.registry, **trustee_kw
        )

    def delivery(self, key: AgentKey | None = None, sender: str = "seller-agent-1") -> Delivery:
        d = Delivery(
            deal_id=self.contract.deal_id, sender=sender, deliverable_ref=GOOD.deliverable_ref, content_hash="ab"
        )
        return d.sign(key or self.sk)

    def attest(self, verdict="accept", key: AgentKey | None = None, **kw) -> Attest:
        a = Attest(deal_id=self.contract.deal_id, sender="buyer-agent-1", verdict=verdict, reason="r", **kw)
        return a.sign(key or self.bk)

    def to_delivered(self) -> None:
        assert self.trustee.on_contract(self.contract).action == "escrowed"
        assert self.trustee.on_delivery(self.delivery(), self.contract).action == "delivered"


def test_signed_honest_deal_settles_and_logs_identity():
    w = Signed()
    w.to_delivered()
    assert w.trustee.on_attest(w.attest(), w.contract).action == "settled"
    assert w.paypal.ops == ["authorize", "capture"]
    ids = [e.payload for e in w.ledger.tail(w.contract.deal_id, kinds=("identity",))]
    assert [i["event"] for i in ids] == ["verify_contract"] * 2 + ["verify_delivery", "verify_attest"]
    assert all(i["ok"] for i in ids)


def test_registered_paypal_email_overrides_default():
    w = Signed()
    w.trustee.on_contract(w.contract)
    assert w.trustee._seller_email == "registered-seller@example.com"
    assert w.trustee._buyer_email == "buyer-sandbox@trustee.dev"


def test_contract_missing_seller_signature_is_refused_before_paypal():
    w = Signed(sign_seller=False)
    d = w.trustee.on_contract(w.contract)
    assert d.action == "rejected_identity"
    assert w.paypal.ops == []
    assert w.trustee.deal.state == DealState.NEGOTIATING
    assert any(e.payload.get("ok") is False for e in w.ledger.tail(w.contract.deal_id, kinds=("identity",)))
    assert not any(e.kind == "mandate" for e in w.ledger.tail(w.contract.deal_id))  # refused before the mandate gate


def test_contract_signed_for_other_terms_is_refused():
    w = Signed()
    repriced = w.contract.model_copy(update={"price": Decimal("1.00")})
    assert w.trustee.on_contract(repriced).action == "rejected_identity"
    assert w.paypal.ops == []


def test_wrong_role_is_refused():
    w = Signed()
    w.registry = AgentRegistry()
    w.registry.register(AgentRecord("buyer-agent-1", w.bk.public_key_b64, "p", frozenset({"seller"})))
    w.registry.register(AgentRecord("seller-agent-1", w.sk.public_key_b64, "p", frozenset({"seller"})))
    w.trustee.registry = w.registry
    assert w.trustee.on_contract(w.contract).action == "rejected_identity"
    assert w.paypal.ops == []


def test_forged_attest_does_not_capture():
    w = Signed()
    w.to_delivered()
    forged = w.attest(key=AgentKey.generate())
    assert w.trustee.on_attest(forged, w.contract).action == "rejected_identity"
    assert (
        w.trustee.on_attest(w.attest().model_copy(update={"signature": None}), w.contract).action == "rejected_identity"
    )
    # the seller cannot attest on the buyer's behalf, even with a valid seller signature
    mine = Attest(deal_id=w.contract.deal_id, sender="seller-agent-1", verdict="accept", reason="r").sign(w.sk)
    assert w.trustee.on_attest(mine, w.contract).action == "rejected_identity"
    assert w.trustee.deal.state == DealState.DELIVERED
    assert w.paypal.ops == ["authorize"]


def test_forged_delivery_is_refused():
    w = Signed()
    assert w.trustee.on_contract(w.contract).action == "escrowed"
    assert w.trustee.on_delivery(w.delivery(key=AgentKey.generate()), w.contract).action == "rejected_identity"
    assert w.trustee.on_delivery(w.delivery(key=w.bk, sender="buyer-agent-1"), w.contract).action == "rejected_identity"
    assert w.trustee.deal.state == DealState.ESCROWED


def test_no_registry_behaves_as_before():
    contract, trustee, _ = make_contract()
    assert trustee.registry is None
    assert trustee.on_contract(contract).action == "escrowed"


def test_budget_is_reserved_committed_and_released():
    w = Signed()
    buyer = w.mandates.get("buyer-agent-1")
    assert buyer is not None
    w.mandates.grant(replace(buyer, max_total=Decimal("200.00"), version=buyer.version + 1).sign(w.pk))
    w.to_delivered()
    assert w.mandates.committed("buyer-agent-1") == Decimal("150.00")
    assert w.trustee.on_attest(w.attest(), w.contract).action == "settled"
    assert w.mandates.committed("buyer-agent-1") == Decimal("150.00")  # now spend, not reservation

    w2 = Signed()
    w2.to_delivered()
    bad = w2.attest("reject", evidence={"sha256_check": "mismatch"})
    assert w2.trustee.on_attest(bad, w2.contract).action == "voided"
    assert w2.mandates.committed("buyer-agent-1") == Decimal("0")


def test_revoked_before_capture_parks_in_dispute_then_operator_settles():
    w = Signed()
    w.to_delivered()
    w.mandates.revoke("buyer-agent-1")
    d = w.trustee.on_attest(w.attest(), w.contract)
    assert d.action == "disputed" and w.trustee.deal.state == DealState.DISPUTED
    assert w.paypal.ops == ["authorize"]  # no capture, no void: funds still held
    rulings = [e.payload for e in w.ledger.tail(w.contract.deal_id, kinds=("ruling",))]
    assert "mandate no longer valid at capture" in rulings[-1]["note"]
    # tick does not move money either
    assert w.trustee.tick(w.contract, now=datetime.now() + timedelta(days=30)).action != "settled"
    assert w.trustee.deal.state != DealState.SETTLED
    # a named operator is the authority that can resolve it
    r = w.trustee.resolve_dispute(w.contract, "settle", operator="ops@example.com", reason="seller delivered")
    assert r.action == "settled" and w.trustee.deal.state == DealState.SETTLED
    assert w.paypal.ops == ["authorize", "capture"]
    final = [e.payload for e in w.ledger.tail(w.contract.deal_id, kinds=("ruling",))][-1]
    assert final["override"] is True and "revoked" in final["mandate_recheck"]


def test_revoked_dispute_can_be_released_by_operator():
    w = Signed()
    w.to_delivered()
    w.mandates.revoke("seller-agent-1")
    assert w.trustee.on_attest(w.attest(), w.contract).action == "disputed"
    r = w.trustee.resolve_dispute(w.contract, "release", operator="ops", reason="no authority")
    assert r.action == "voided" and w.paypal.ops == ["authorize", "void"]


# ----------------------------------------------------------------- snapshots


def roundtrip(w: Signed) -> Trustee:
    import json

    snap = json.loads(json.dumps(w.trustee.snapshot()))  # must survive real JSON
    return Trustee.from_snapshot(snap, mandates=w.mandates, paypal=w.paypal, ledger=w.ledger, registry=w.registry)


def test_snapshot_restore_escrowed_then_finish():
    w = Signed()
    assert w.trustee.on_contract(w.contract).action == "escrowed"
    t2 = roundtrip(w)
    assert t2.deal.state == DealState.ESCROWED and t2.authorized_at == w.trustee.authorized_at
    assert t2.on_delivery(w.delivery(), w.contract).action == "delivered"
    assert t2.on_attest(w.attest(), w.contract).action == "settled"
    assert w.paypal.ops == ["authorize", "capture"]


def test_snapshot_restore_delivered_then_cheat_voids():
    w = Signed()
    w.to_delivered()
    t2 = roundtrip(w)
    assert t2.deal.state == DealState.DELIVERED and t2._last_delivery == w.trustee._last_delivery
    assert t2.on_attest(w.attest("reject", evidence={"sha256_check": "mismatch"}), w.contract).action == "voided"


def test_snapshot_restore_disputed_keeps_ruling_and_deadline():
    w = Signed()
    w.to_delivered()
    assert w.trustee.on_attest(w.attest("reject", evidence={}), w.contract).action == "disputed"
    t2 = roundtrip(w)
    assert t2.deal.state == DealState.DISPUTED
    assert t2._ruling == w.trustee._ruling and t2.dispute_deadline == w.trustee.dispute_deadline
    assert t2.deal.history == w.trustee.deal.history
    # past the dispute window the restored Trustee captures the upheld dispute on its own
    late = t2.dispute_deadline + timedelta(seconds=1)  # type: ignore[operator]
    assert t2.tick(w.contract, now=late).action == "settled"


def test_deal_restore_does_not_announce_transitions():
    seen: list[tuple] = []
    deal = Deal.restore(
        "d", DealState.ESCROWED, [("r", "CONTRACTED", "ESCROWED")], on_transition=lambda *a: seen.append(a)
    )
    assert seen == [] and deal.state == DealState.ESCROWED
    deal.transition(DealState.DELIVERED, "x")
    assert len(seen) == 1 and len(deal.history) == 2


def test_snapshot_of_unopened_deal_round_trips():
    contract, trustee, _ = make_contract()
    snap = trustee.snapshot()
    assert snap["escrow"] is None and snap["state"] == "NEGOTIATING"
    t2 = Trustee.from_snapshot(snap, mandates=trustee.mandates, paypal=trustee.paypal)
    assert t2.on_contract(contract).action == "escrowed"


def test_unregistered_parties_are_refused():
    w = Signed()
    w.trustee.registry = AgentRegistry()
    assert w.trustee.on_contract(w.contract).action == "rejected_identity"
