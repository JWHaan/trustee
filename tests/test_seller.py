"""Seller agent tests + the cross-agent cheat flow (seller -> buyer)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from trustee.agents.buyer import ArtifactStore, BuyerAgent
from trustee.agents.seller import SellerAgent
from trustee.ledger import Ledger
from trustee.llm import LLM
from trustee.protocol import Contract, Counter, GoodSpec

GOOD = GoodSpec(
    good_id="lambda-module-1",
    description="Lambdas math module, tested",
    category="code",
    acceptance_criteria=["sha256 in manifest", "tests pass"],
    deliverable_ref="lambda-module.zip",
)


class FakeLLM(LLM):
    def __init__(self, replies: list[str]):
        super().__init__(model="fake")
        self.replies = list(replies)

    def complete(self, system, messages):
        return self.replies.pop(0)


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    goods = tmp_path / "goods"
    goods.mkdir()
    honest = goods / "lambda-module.zip"
    honest.write_bytes(b"LAMBDA_OK")
    manifest = {GOOD.deliverable_ref: hashlib.sha256(b"LAMBDA_OK").hexdigest()}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return ArtifactStore.load(tmp_path)


def make_seller(store: ArtifactStore, llm: FakeLLM) -> SellerAgent:
    return SellerAgent(
        agent_id="seller-agent-1",
        llm=llm,
        good=GOOD,
        floor_price=Decimal("100.00"),
        target_price=Decimal("180.00"),
        store=store,
        ledger=Ledger(),
    )


def test_accepts_offer_at_or_above_floor(store: ArtifactStore):
    seller = make_seller(store, FakeLLM([]))  # LLM never needed
    from trustee.protocol import Accept, Offer

    offer = Offer.new(
        sender="buyer-agent-1",
        good=GOOD,
        price=Decimal("120.00"),
    )
    result = seller.respond_to(offer)
    assert isinstance(result, Accept)
    assert result.price == Decimal("120.00")
    assert result.sender == "seller-agent-1"


def test_llm_counter_clamped_to_floor(store: ArtifactStore):
    # LLM proposes below floor — the hard clamp must hold the line.
    llm = FakeLLM([json.dumps({"kind": "counter", "price": 10.0})])
    seller = make_seller(store, llm)
    from trustee.protocol import Offer

    offer = Offer.new(sender="buyer-agent-1", good=GOOD, price=Decimal("50.00"))
    result = seller.respond_to(offer)
    assert isinstance(result, Counter)
    assert result.price == Decimal("100.00")  # floor, not the LLM's 10.00


def test_llm_counter_respected_within_bounds(store: ArtifactStore):
    llm = FakeLLM([json.dumps({"kind": "counter", "price": 115.0})])
    seller = make_seller(store, llm)
    from trustee.protocol import Offer

    offer = Offer.new(sender="buyer-agent-1", good=GOOD, price=Decimal("90.00"))
    result = seller.respond_to(offer)
    assert isinstance(result, Counter)
    assert result.price == Decimal("115.00")


def test_llm_outage_falls_back_to_floor_counter(store: ArtifactStore):
    llm = FakeLLM(["garbage"] * 5)
    seller = make_seller(store, llm)
    from trustee.protocol import Offer

    offer = Offer.new(sender="buyer-agent-1", good=GOOD, price=Decimal("50.00"))
    result = seller.respond_to(offer)
    assert isinstance(result, Counter)
    assert result.price == seller.floor_price


def test_honest_delivery_hash_matches_manifest(store: ArtifactStore):
    seller = make_seller(store, FakeLLM([]))
    contract = _contract()
    delivery = seller.deliver(contract)
    assert delivery.content_hash == store.manifest[GOOD.deliverable_ref]
    assert delivery.deliverable_ref == GOOD.deliverable_ref


def test_cheat_delivery_hash_mismatches_manifest(store: ArtifactStore):
    junk = store.goods_dir / f"{GOOD.good_id}-junk.bin"
    junk.write_bytes(b"LAMBDA_CORRUPT")
    seller = make_seller(store, FakeLLM([]))
    contract = _contract()
    delivery = seller.deliver(contract, cheat=True)
    assert delivery.content_hash != store.manifest[GOOD.deliverable_ref]


def test_cross_agent_cheat_flow(store: ArtifactStore):
    """The whole demo run 2: seller cheats, buyer's machine check rejects."""
    junk = store.goods_dir / f"{GOOD.good_id}-junk.bin"
    junk.write_bytes(b"LAMBDA_CORRUPT")
    contract = _contract()
    seller = make_seller(store, FakeLLM([]))

    buyer = BuyerAgent(
        agent_id="buyer-agent-1",
        llm=FakeLLM([]),
        good=GOOD,
        budget=Decimal("200.00"),
        store=store,
        checkers={},
        ledger=Ledger(),
    )
    delivery = seller.deliver(contract, cheat=True)
    attest = buyer.verify_delivery(contract, delivery)
    assert attest.verdict == "reject"
    assert attest.evidence["sha256_check"] == "mismatch"


def _contract() -> Contract:
    from datetime import datetime

    now = datetime.now()
    return Contract(
        deal_id="deal_seller_test",
        buyer="buyer-agent-1",
        seller="seller-agent-1",
        good=GOOD,
        price=Decimal("120.00"),
        deadline=now,
        formed_at=now,
    )
