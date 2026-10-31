"""Buyer-side delivery verification: the buyer checks bytes it received against a hash fixed in the contract.

The seller's reported hash is a claim, not evidence. These tests pin the rules that make the buyer
verify the delivered bytes itself, and that keep the negotiating LLM from changing contract terms.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trustee.agents.arbiter import ArbiterAgent
from trustee.agents.buyer import ArtifactStore, BuyerAgent
from trustee.agents.negotiator import Negotiator
from trustee.llm import LLM
from trustee.protocol import Contract, Delivery, GoodSpec

HONEST = b"HONEST_MODULE"
JUNK = b"JUNK_MODULE"
HONEST_HASH = hashlib.sha256(HONEST).hexdigest()
JUNK_HASH = hashlib.sha256(JUNK).hexdigest()
REF = "lambda-module.zip"


def make_good(sha256: str | None = HONEST_HASH) -> GoodSpec:
    return GoodSpec(
        good_id="lambda-module-1",
        description="Lambdas math module, tested",
        category="code",
        acceptance_criteria=["sha256 in manifest"],
        deliverable_ref=REF,
        sha256=sha256,
    )


def make_contract(good: GoodSpec) -> Contract:
    now = datetime.now()
    return Contract(
        deal_id="deal_verify",
        buyer="buyer-agent-1",
        seller="seller-agent-1",
        good=good,
        price=Decimal("156.00"),
        deadline=now,
        formed_at=now,
    )


def make_delivery(content_hash: str, ref: str = REF) -> Delivery:
    return Delivery(sender="seller-agent-1", deal_id="deal_verify", deliverable_ref=ref, content_hash=content_hash)


class ScriptedLLM(LLM):
    """Returns canned JSON for every call. No network."""

    def __init__(self, reply: str):
        super().__init__(model="fake")
        self.reply = reply

    def complete(self, system, messages):
        return self.reply

    def complete_json(self, system, messages):
        return json.loads(self.reply)


def make_store(tmp_path: Path, manifest: dict[str, str] | None, received: bytes | None) -> ArtifactStore:
    goods = tmp_path / "goods"
    inbox = tmp_path / "inbox"
    goods.mkdir()
    inbox.mkdir()
    (goods / REF).write_bytes(HONEST)
    if received is not None:
        (inbox / REF).write_bytes(received)
    return ArtifactStore(goods_dir=goods, manifest=manifest or {}, inbox=inbox)


def make_buyer(store: ArtifactStore) -> BuyerAgent:
    return BuyerAgent(
        agent_id="buyer-agent-1",
        llm=ScriptedLLM(json.dumps({"summary": "checked"})),
        good=make_good(),
        budget=Decimal("200.00"),
        store=store,
    )


# ------------------------------------------------------------------ verdicts


def test_lying_seller_is_rejected_and_arbiter_treats_it_as_objective(tmp_path: Path):
    # Seller reports the honest hash but ships junk bytes.
    store = make_store(tmp_path, manifest={}, received=JUNK)
    contract = make_contract(make_good(HONEST_HASH))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH))

    assert attest.verdict == "reject"
    assert attest.evidence["sha256_check"] == "mismatch"
    assert attest.evidence["sha256_received"] == JUNK_HASH
    assert attest.evidence["sha256_expected"] == HONEST_HASH

    ruling = ArbiterAgent("arbiter-agent-1", llm=None).rule(contract, make_delivery(HONEST_HASH), attest)
    assert ruling.decision == "release"
    assert ruling.clause == "sha256 in manifest"
    assert ruling.objective is True


def test_honest_delivery_is_accepted_against_contract_hash(tmp_path: Path):
    store = make_store(tmp_path, manifest={}, received=HONEST)
    contract = make_contract(make_good(HONEST_HASH))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH))

    assert attest.verdict == "accept"
    assert attest.evidence["sha256_check"] == "match"
    assert attest.evidence["sha256_source"] == "contract"


def test_contract_hash_overrides_manifest(tmp_path: Path):
    # A manifest that disagrees with the contract must not be consulted.
    store = make_store(tmp_path, manifest={REF: JUNK_HASH}, received=HONEST)
    contract = make_contract(make_good(HONEST_HASH))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH))

    assert attest.verdict == "accept"
    assert attest.evidence["sha256_source"] == "contract"


def test_legacy_good_without_hash_falls_back_to_manifest(tmp_path: Path):
    store = make_store(tmp_path, manifest={REF: HONEST_HASH}, received=HONEST)
    contract = make_contract(make_good(sha256=None))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH))

    assert attest.verdict == "accept"
    assert attest.evidence["sha256_source"] == "manifest"
    assert "sha256_expected" in attest.evidence


def test_no_expected_hash_anywhere_is_unknown_ref(tmp_path: Path):
    store = make_store(tmp_path, manifest={}, received=HONEST)
    contract = make_contract(make_good(sha256=None))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH))

    assert attest.verdict == "reject"
    assert attest.evidence["sha256_check"] == "unknown_ref"
    assert "no expected sha256" in attest.reason


def test_missing_inbox_file_is_rejected_as_nothing_delivered(tmp_path: Path):
    store = make_store(tmp_path, manifest={}, received=None)
    contract = make_contract(make_good(HONEST_HASH))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH))

    assert attest.verdict == "reject"
    assert attest.evidence["sha256_check"] == "mismatch"
    assert "nothing delivered" in attest.reason


def test_deliverable_ref_mismatch_is_rejected(tmp_path: Path):
    store = make_store(tmp_path, manifest={}, received=HONEST)
    contract = make_contract(make_good(HONEST_HASH))
    attest = make_buyer(store).verify_delivery(contract, make_delivery(HONEST_HASH, ref="other.zip"))

    assert attest.verdict == "reject"
    assert "contract names" in attest.reason


def test_checker_still_runs_on_received_bytes(tmp_path: Path):
    seen: list[bytes] = []

    def checker(artifact: Path, _good_id: str) -> tuple[bool, str]:
        seen.append(artifact.read_bytes())
        return True, "tests pass"

    store = make_store(tmp_path, manifest={}, received=HONEST)
    buyer = make_buyer(store)
    buyer.checkers = {"lambda-module-1": checker}
    buyer.verify_delivery(make_contract(make_good(HONEST_HASH)), make_delivery(HONEST_HASH))

    assert seen == [HONEST]


# ------------------------------------------------------ negotiator cannot edit terms


def test_llm_emitted_good_cannot_change_the_coerced_message():
    agent_good = make_good(HONEST_HASH)
    forged = {
        "kind": "offer",
        "price": 140.0,
        "good": {**agent_good.model_dump(mode="json"), "deliverable_ref": "evil.zip", "sha256": JUNK_HASH},
    }
    negotiator = Negotiator(
        agent_id="buyer-agent-1",
        role="buyer",
        llm=ScriptedLLM(json.dumps(forged)),
        good=agent_good,
    )
    offer = negotiator.open_deal(Decimal("140.00"), "seller-agent-1")

    assert offer.good == agent_good
    assert offer.good.sha256 == HONEST_HASH
    assert offer.good.deliverable_ref == REF


# ------------------------------------------------------------------ protocol


def test_goodspec_rejects_malformed_sha256():
    for bad in ("ABCD", "A" * 64, "g" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValidationError):
            make_good(bad)


def test_goodspec_accepts_none_and_valid_hash():
    assert make_good(None).sha256 is None
    assert make_good(HONEST_HASH).sha256 == HONEST_HASH


def test_key_clauses_carry_sha256_only_when_set():
    with_hash = make_contract(make_good(HONEST_HASH)).key_clauses()
    without = make_contract(make_good(None)).key_clauses()
    assert with_hash["sha256"] == HONEST_HASH
    assert "sha256" not in without
