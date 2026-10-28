"""Buyer agent tests: constrained negotiation + machine-checked attestation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from trustee.agents.buyer import ArtifactStore, BuyerAgent
from trustee.ledger import Ledger
from trustee.llm import LLM
from trustee.protocol import Accept, Delivery, GoodSpec

GOOD = GoodSpec(
    good_id="lambda-module-1",
    description="Lambdas math module, tested",
    category="code",
    acceptance_criteria=["sha256 in manifest", "tests pass"],
    deliverable_ref="lambda-module.zip",
)


class FakeLLM(LLM):
    """Scripted LLM: returns canned JSON moves, no network."""

    def __init__(self, replies: list[str]):
        super().__init__(model="fake")
        self.replies = list(replies)

    def complete(self, system, messages):
        return self.replies.pop(0)

    def complete_json(self, system, messages):
        return json.loads(self.complete(system, messages))


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    goods = tmp_path / "goods"
    goods.mkdir()
    honest = goods / "lambda-module-1.bin"
    honest.write_bytes(b"LAMBDA_OK")
    manifest = {GOOD.deliverable_ref: hashlib.sha256(b"LAMBDA_OK").hexdigest()}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return ArtifactStore.load(tmp_path)


def make_buyer(store: ArtifactStore, llm: FakeLLM) -> BuyerAgent:
    def tests_pass(artifact: Path, _good_id: str) -> tuple[bool, str]:
        ok = artifact.exists() and b"LAMBDA_OK" in artifact.read_bytes()
        return ok, "tests pass" if ok else "tests failed"

    return BuyerAgent(
        agent_id="buyer-agent-1",
        llm=llm,
        good=GOOD,
        budget=Decimal("200.00"),
        store=store,
        checkers={"lambda-module-1": tests_pass},
        ledger=Ledger(),
    )


def test_open_deal_produces_valid_offer(store: ArtifactStore):
    llm = FakeLLM([json.dumps({"kind": "offer", "price": 120.0, "good": GOOD.model_dump(mode="json")})])
    buyer = make_buyer(store, llm)
    offer = buyer.open_deal(buyer.opening_price(Decimal("150.00")), "seller-agent-1")
    assert offer.kind == "offer"
    assert offer.price == buyer.opening_price(Decimal("150.00"))
    assert offer.sender == "buyer-agent-1"
    assert offer.good.good_id == GOOD.good_id


def test_cap_hit_falls_back_to_deterministic_accept(store: ArtifactStore):
    # Every LLM reply is invalid JSON -> all turns fail -> cap fallback
    llm = FakeLLM(["not json at all"] * 10)
    buyer = make_buyer(store, llm)
    result = buyer.respond({"kind": "offer", "price": 150}, open_price=Decimal("120.00"), max_turns=3)
    assert isinstance(result, Accept)
    assert result.price == Decimal("120.00")
    assert result.responds_to == "cap"


def test_honest_delivery_attests_accept(store: ArtifactStore):
    llm = FakeLLM([json.dumps({"summary": "Delivery verified against both clauses."})])
    buyer = make_buyer(store, llm)
    delivery = Delivery(
        sender="seller-agent-1",
        deal_id="deal_test",
        deliverable_ref=GOOD.deliverable_ref,
        content_hash=store.manifest[GOOD.deliverable_ref],
    )
    attest = buyer.verify_delivery(contract_from_test(), delivery)
    assert attest.verdict == "accept"
    assert attest.evidence["sha256_check"] == "match"
    assert attest.evidence["checker"] == "tests pass"


def test_cheat_delivery_attests_reject_with_evidence(store: ArtifactStore):
    llm = FakeLLM([json.dumps({"summary": "Delivery failed manifest check."})])
    buyer = make_buyer(store, llm)
    delivery = Delivery(
        sender="seller-agent-1",
        deal_id="deal_test",
        deliverable_ref=GOOD.deliverable_ref,
        content_hash="deadbeef",  # junk hash
    )
    attest = buyer.verify_delivery(contract_from_test(), delivery)
    assert attest.verdict == "reject"
    assert attest.evidence["sha256_check"] == "mismatch"
    assert "manifest" in attest.reason


def test_reject_reason_survives_llm_outage(store: ArtifactStore):
    # LLM explodes: deterministic reason must still carry the clause
    class BoomLLM(FakeLLM):
        def complete_json(self, system, messages):
            raise RuntimeError("model down")

    buyer = make_buyer(store, BoomLLM([]))
    delivery = Delivery(
        sender="seller-agent-1",
        deal_id="deal_test",
        deliverable_ref=GOOD.deliverable_ref,
        content_hash="deadbeef",
    )
    attest = buyer.verify_delivery(contract_from_test(), delivery)
    assert attest.verdict == "reject"
    assert "sha256 does not match manifest" in attest.reason


def contract_from_test():
    from trustee.protocol import Contract

    now = __import__("datetime").datetime.now()
    return Contract(
        deal_id="deal_test",
        buyer="buyer-agent-1",
        seller="seller-agent-1",
        good=GOOD,
        price=Decimal("120.00"),
        deadline=now,
        formed_at=now,
    )
