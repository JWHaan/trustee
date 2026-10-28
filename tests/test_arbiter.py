"""Arbiter tests: objective breaches, LLM-assisted rulings, judge-wired Trustee."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trustee.agents.arbiter import ArbiterAgent
from trustee.agents.trustee import Trustee
from trustee.ledger import Ledger
from trustee.mandate import MandateEngine, demo_mandates
from trustee.paypal import PayPalClient
from trustee.protocol import Attest, Contract, Delivery, GoodSpec
from trustee.state import Deal, DealState

GOOD = GoodSpec(
    good_id="lambda-module-1",
    description="Lambdas math module, tested",
    category="code",
    acceptance_criteria=["sha256 in manifest", "tests pass"],
    deliverable_ref="lambda-module.zip",
)


def make_contract(price: str = "150.00") -> Contract:
    now = datetime.now()
    return Contract(
        deal_id="deal_arbiter",
        buyer="buyer-agent-1",
        seller="seller-agent-1",
        good=GOOD,
        price=Decimal(price),
        deadline=now,
        formed_at=now,
    )


def make_delivery(cheat: bool = False) -> Delivery:
    return Delivery(
        sender="seller-agent-1",
        deal_id="deal_arbiter",
        deliverable_ref=GOOD.deliverable_ref,
        content_hash="badbad" if cheat else "goodgood",
    )


def attest_reject(evidence: dict) -> Attest:
    return Attest(
        sender="buyer-agent-1",
        deal_id="deal_arbiter",
        verdict="reject",
        reason="delivery does not meet contract",
        evidence=evidence,
    )


def test_hash_mismatch_is_objective_release_without_llm():
    arbiter = ArbiterAgent("arbiter-1", llm=None, ledger=Ledger())
    ruling = arbiter.rule(make_contract(), make_delivery(cheat=True), attest_reject({"sha256_check": "mismatch"}))
    assert ruling.decision == "release"
    assert ruling.clause == "sha256 in manifest"
    assert ruling.objective and not ruling.llm_assisted


def test_checker_failure_is_objective_release_without_llm():
    arbiter = ArbiterAgent("arbiter-1", llm=None, ledger=Ledger())
    ruling = arbiter.rule(
        make_contract(), make_delivery(), attest_reject({"sha256_check": "match", "checker": "tests failed"})
    )
    assert ruling.decision == "release"
    assert ruling.clause == "tests pass"


def test_unsubstantiated_reject_upholds():
    # No evidence at all: the LLM (if any) judges; without an LLM, money stays held.
    arbiter = ArbiterAgent("arbiter-1", llm=None, ledger=Ledger())
    ruling = arbiter.rule(make_contract(), make_delivery(), attest_reject({}))
    assert ruling.decision == "uphold"


def test_llm_judgment_on_gray_evidence():
    from trustee.llm import LLM

    class FakeLLM(LLM):
        def __init__(self):
            super().__init__(model="fake")

        def complete(self, system, messages):
            raise AssertionError("complete should not be called")

        def complete_json(self, system, messages):
            if "impartial judge" in system:
                return {"decision": "release"}
            return {"rationale": "sha256 in manifest clause violated"}

    arbiter = ArbiterAgent("arbiter-1", llm=FakeLLM(), ledger=Ledger())
    ruling = arbiter.rule(
        make_contract(),
        make_delivery(),
        attest_reject({"sha256_check": "match", "note": "file size too small"}),
    )
    assert ruling.decision == "release"
    assert ruling.llm_assisted
    assert "manifest" in ruling.reasoning
    assert ruling.objective is False


def test_llm_outage_falls_back_to_uphold():
    from trustee.llm import LLM

    class BoomLLM(LLM):
        def __init__(self):
            super().__init__(model="fake")

        def complete_json(self, system, messages):
            raise RuntimeError("model down")

    arbiter = ArbiterAgent("arbiter-1", llm=BoomLLM(), ledger=Ledger())
    ruling = arbiter.rule(
        make_contract(),
        make_delivery(),
        attest_reject({"note": "subjective quality complaint"}),
    )
    assert ruling.decision == "uphold"
    assert ruling.llm_assisted


def test_trustee_wired_to_judge_voids_on_objective_breach():
    contract = make_contract()
    deal = Deal(contract.deal_id)
    deal.transition(DealState.NEGOTIATING, "start")
    ledger = Ledger()
    trustee = Trustee(
        deal,
        MandateEngine(demo_mandates()),
        PayPalClient(dry_run=True),
        ledger=ledger,
        judge=ArbiterAgent("arbiter-1", llm=None, ledger=ledger),
    )
    d = trustee.on_contract(contract)
    assert d.action == "escrowed"
    delivery = make_delivery(cheat=True)
    trustee.on_delivery(delivery, contract)
    d = trustee.on_attest(attest_reject({"sha256_check": "mismatch"}), contract)
    assert d.action == "voided"
    assert deal.state == DealState.VOIDED
    kinds = [e.kind for e in ledger.tail(contract.deal_id)]
    assert "ruling" in kinds


def test_trustee_wired_to_judge_holds_escrow_on_unsubstantiated_reject():
    contract = make_contract()
    deal = Deal(contract.deal_id)
    deal.transition(DealState.NEGOTIATING, "start")
    trustee = Trustee(
        deal,
        MandateEngine(demo_mandates()),
        PayPalClient(dry_run=True),
        judge=ArbiterAgent("arbiter-1", llm=None, ledger=Ledger()),
    )
    trustee.on_contract(contract)
    trustee.on_delivery(make_delivery(), contract)
    d = trustee.on_attest(attest_reject({}), contract)
    assert d.action == "disputed"
    assert deal.state == DealState.DISPUTED  # funds still in escrow
