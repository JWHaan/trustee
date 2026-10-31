"""Buyer agent — negotiate, then verify delivery and attest.

The buyer's attestation is the evidence the Trustee needs to capture or void:
machine-checkable (hash of the received bytes vs. the contract hash, registered
checkers) with the LLM only
summarizing. A `cheat` fixture ships a corrupt deliverable so the reject path
is demonstrable end-to-end.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from ..ledger import Ledger
from ..llm import LLM
from ..protocol import Attest, Contract, Delivery, GoodSpec
from .negotiator import Negotiator

log = logging.getLogger(__name__)

Checker = Callable[[Path, str], tuple[bool, str]]  # (artifact, good_id) -> (ok, detail)


@dataclass
class ArtifactStore:
    """The seller's inventory and the buyer's inbox.

    `goods_dir` is where artifacts live *before* delivery; `inbox` is where a
    delivery lands. They are deliberately separate: if both sides shared one
    directory, a cheating seller would either have to corrupt the fixture it
    was supposed to ship, or the buyer's checker would end up inspecting the
    honest file and reporting "pass" on a junk delivery. Two directories make
    the transfer real, so criterion 2 reads the bytes that were actually sent.
    """

    goods_dir: Path
    manifest: dict[str, str] = field(default_factory=dict)
    inbox: Path | None = None

    @classmethod
    def load(cls, root: Path, inbox: Path | None = None) -> ArtifactStore:
        manifest = json.loads((root / "manifest.json").read_text())
        return cls(goods_dir=root / "goods", manifest=manifest, inbox=inbox)

    @property
    def inbox_dir(self) -> Path:
        return self.inbox if self.inbox is not None else self.goods_dir

    def hash_artifact(self, ref: str) -> str:
        p = self.goods_dir / ref
        if not p.exists():
            return ""
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def receive(self, ref: str, payload: bytes) -> str:
        """Land delivered bytes in the inbox; return their hash."""
        dest = self.inbox_dir / ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def received(self, ref: str) -> Path:
        return self.inbox_dir / ref


class BuyerAgent(Negotiator):
    def __init__(
        self,
        agent_id: str,
        llm: LLM,
        good: GoodSpec,
        budget: Decimal,
        store: ArtifactStore,
        checkers: dict[str, Checker] | None = None,
        ledger: Ledger | None = None,
    ):
        super().__init__(agent_id=agent_id, role="buyer", llm=llm, good=good)
        self.budget = budget
        self.checkers = checkers or {}
        self.store = store
        self.ledger = ledger

    # -------------------------------------------------------- negotiation side

    def opening_price(self, ask: Decimal) -> Decimal:
        """Start ~20% under the ask, but never over budget."""
        return min((ask * Decimal("0.8")).quantize(Decimal("0.01")), self.budget)

    # ---------------------------------------------------------- delivery side

    def verify_delivery(self, contract: Contract, delivery: Delivery) -> Attest:
        """Machine-check the delivery against the contract, then attest.

        The buyer hashes the bytes it received itself. A seller-reported hash
        is only a claim: the seller controls it, and it could describe a file
        other than the one that arrived. The expected hash comes from the
        contract when it names one, so both parties fixed it before delivery.
        The manifest is a legacy fallback for goods formed without a hash.
        """
        evidence: dict[str, str] = {}
        failures: list[str] = []
        ref = contract.good.deliverable_ref
        reported = delivery.content_hash
        evidence["sha256_reported"] = reported

        if delivery.deliverable_ref != ref:
            failures.append(f"delivered '{delivery.deliverable_ref}' but contract names '{ref}'")

        # 1. the bytes we received must hash to the hash both parties fixed
        expected: str | None
        if contract.good.sha256 is not None:
            expected = contract.good.sha256
            evidence["sha256_source"] = "contract"
        elif ref in self.store.manifest:
            expected = self.store.manifest[ref]
            evidence["sha256_source"] = "manifest"
        else:
            expected = None

        received_path = self.store.received(ref)
        if expected is None:
            evidence["sha256_check"] = "unknown_ref"
            failures.append(f"deliverable '{ref}' has no expected sha256 in the contract or manifest")
        elif not received_path.is_file():
            evidence["sha256_expected"] = expected
            evidence["sha256_check"] = "mismatch"
            failures.append("nothing delivered")
        else:
            received = hashlib.sha256(received_path.read_bytes()).hexdigest()
            evidence["sha256_expected"] = expected
            evidence["sha256_received"] = received
            if received != expected:
                evidence["sha256_check"] = "mismatch"
                failures.append(f"sha256 does not match {evidence['sha256_source']}")
            elif reported != received:
                # The bytes are correct, but the seller's claim about them is false.
                evidence["sha256_check"] = "mismatch"
                failures.append("seller-reported hash does not match delivered bytes")
            else:
                evidence["sha256_check"] = "match"

        # 2. registered checkers per good (e.g. "schema: order_id, sku, ...")
        #    Runs against the bytes that actually arrived, not a path named
        #    after the good — otherwise this criterion never inspects the
        #    delivery at all.
        checker = self.checkers.get(contract.good.good_id)
        if checker is not None:
            ok, detail = checker(received_path, contract.good.good_id)
            evidence["checker"] = detail
            if not ok:
                failures.append(detail)

        verdict: Literal["accept", "reject"] = "accept" if not failures else "reject"
        reason = self._summarize(failures or ["all acceptance criteria met"])
        attest = Attest(
            sender=self.agent_id,
            deal_id=contract.deal_id,
            verdict=verdict,
            reason=reason,
            evidence=evidence,
        )
        if self.ledger is not None:
            dump = attest.model_dump(mode="json")
            dump.pop("deal_id", None)
            dump.pop("kind", None)
            self.ledger.append(contract.deal_id, self.agent_id, "msg", event="attestation", **dump)
        return attest

    # ------------------------------------------------------------ internals

    def _summarize(self, notes: list[str]) -> str:
        joined = "; ".join(notes)
        if self.llm is None:
            return joined
        prompt = f"Verdict inputs: {joined}. Reply JSON: {json.dumps({'summary': 'str'})}"
        try:
            text = self.llm.complete_json(
                "Summarize a delivery verdict in one short sentence for a ledger entry.",
                [{"role": "user", "content": prompt}],
            )
            summary = str(text.get("summary", "")).strip()
            return f"{summary} ({joined})" if summary else joined
        except Exception:  # noqa: BLE001 - deterministic reason must always exist
            return joined
