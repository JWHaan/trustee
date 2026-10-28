"""Buyer agent — negotiate, then verify delivery and attest.

The buyer's attestation is the evidence the Trustee needs to capture or void:
machine-checkable (hash vs. manifest, registered checkers) with the LLM only
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

from ..ledger import Ledger
from ..llm import LLM
from ..protocol import Attest, Contract, Delivery, GoodSpec
from .negotiator import Negotiator

log = logging.getLogger(__name__)

Checker = Callable[[Path, str], tuple[bool, str]]  # (artifact, good_id) -> (ok, detail)


@dataclass
class ArtifactStore:
    """Demo deliverables: a goods dir + a manifest of expected sha256 values."""

    goods_dir: Path
    manifest: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> ArtifactStore:
        manifest = json.loads((root / "manifest.json").read_text())
        return cls(goods_dir=root / "goods", manifest=manifest)

    def hash_artifact(self, ref: str) -> str:
        p = self.goods_dir / ref
        if not p.exists():
            return ""
        return hashlib.sha256(p.read_bytes()).hexdigest()


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
        """Machine-check the delivery against the contract, then attest."""
        evidence: dict[str, str] = {}
        failures: list[str] = []

        # 1. hash must match the manifest (the "sha256 in manifest" clause)
        if delivery.deliverable_ref in self.store.manifest:
            expected = self.store.manifest[delivery.deliverable_ref]
            ok = delivery.content_hash == expected
            evidence["sha256_check"] = "match" if ok else "mismatch"
            if not ok:
                failures.append("sha256 does not match manifest")
        else:
            evidence["sha256_check"] = "unknown_ref"
            failures.append(f"deliverable '{delivery.deliverable_ref}' not in manifest")

        # 2. registered checkers per good (e.g. "pytest suite passes")
        checker = self.checkers.get(contract.good.good_id)
        if checker is not None:
            artifact = self.store.goods_dir / f"{contract.good.good_id}.bin"
            ok, detail = checker(artifact, contract.good.good_id)
            evidence["checker"] = detail
            if not ok:
                failures.append(detail)

        verdict = "accept" if not failures else "reject"
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
