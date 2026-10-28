"""End-to-end Trustee flows: honest deal settles, cheat deal refunds."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from trustee.agents.trustee import Trustee
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
