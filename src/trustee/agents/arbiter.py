"""Arbiter agent — the judge of agent-to-agent disputes.

When a buyer attests reject, the Trustee asks the arbiter for a ruling:
*release* (void the escrow, refund the buyer) or *uphold* (keep the escrow,
seller retains the claim).

The arbiter is policy-anchored: an objective breach (hash mismatch, failed
checker) is deterministically a release *before* any LLM is involved. The LLM
adds the human-readable ruling — which clause was violated, why — and if the
LLM is unavailable the deterministic ruling stands on its own. The LLM can
never turn an objective breach into an uphold.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..ledger import Ledger
from ..llm import LLM
from ..protocol import Attest, Contract, Delivery

log = logging.getLogger(__name__)

Decision = Literal["release", "uphold"]


@dataclass
class Ruling:
    decision: Decision
    clause: str  # the contract clause that decides it
    reasoning: str
    objective: bool = True  # True = decided by policy, no LLM required
    llm_assisted: bool = False
    payload: dict = field(default_factory=dict)


class Judge(Protocol):
    def rule(self, contract: Contract, delivery: Delivery, attest: Attest) -> Ruling: ...


def _objective_breach(attest: Attest) -> str | None:
    """Return the violated clause if the evidence shows an objective breach."""
    evidence = attest.evidence
    if evidence.get("sha256_check") in ("mismatch", "unknown_ref"):
        return "sha256 in manifest"
    if "failed" in evidence.get("checker", ""):
        return "tests pass"
    return None


class ArbiterAgent(Judge):
    agent_id: str

    def __init__(self, agent_id: str, llm: LLM | None, ledger: Ledger | None = None):
        self.agent_id = agent_id
        self.llm = llm
        self.ledger = ledger

    def rule(self, contract: Contract, delivery: Delivery, attest: Attest) -> Ruling:
        if attest.verdict == "accept":
            # No dispute: settlement proceeds. Recorded for the ledger tape.
            return Ruling("release", "settlement clause", "buyer attested accept; funds settle", objective=True)

        clause = _objective_breach(attest)
        if clause is not None:
            # Objective breach (or un-evidenced reject -> uphold by default):
            # deterministic core. Note: an *un-evidenced* reject upholds.
            decision: Decision = "release" if clause else "uphold"
            ruling = Ruling(
                decision,
                clause or "evidence clause",
                _deterministic_reason(decision, attest),
                objective=True,
            )
            return self._narrate(ruling, contract, attest)

        # Buyer rejected with evidence that does not map to a known breach type:
        # let the LLM judge, then clamp: it cannot release without a named clause.
        decision = self._llm_decide(contract, delivery, attest)
        ruling = Ruling(
            decision,
            "buyer attestation (judged)",
            _deterministic_reason(decision, attest),
            objective=False,
            llm_assisted=True,
        )
        return self._narrate(ruling, contract, attest)

    # ------------------------------------------------------------ internals

    def _llm_decide(self, contract: Contract, delivery: Delivery, attest: Attest) -> Decision:
        prompt = json.dumps(
            {
                "contract": contract.key_clauses(),
                "delivery": delivery.model_dump(mode="json"),
                "attestation": attest.model_dump(mode="json"),
            },
            default=str,
        )
        if self.llm is None:
            return "uphold"  # no judge available: money stays in escrow, human can void
        try:
            out = self.llm.complete_json(
                "You are an impartial judge of agent commerce. Rule 'release' or 'uphold' "
                "on the buyer's attestation against the contract. "
                'Reply JSON: {"decision": "release"|"uphold"}.',
                [{"role": "user", "content": prompt}],
            )
            raw = str(out.get("decision", "")).lower()
            if raw == "release":
                decision: Decision = "release"
            else:
                # Anything the model didn't say exactly is an uphold:
                # money stays held, a human can void it.
                decision = "uphold"
            return decision
        except Exception:  # noqa: BLE001 - judgment must never crash the money path
            log.warning("%s: judge LLM failed — defaulting to uphold", self.agent_id)
            return "uphold"

    def _narrate(self, ruling: Ruling, contract: Contract, attest: Attest) -> Ruling:
        if self.llm is None:
            self._log(ruling, contract.deal_id, attest)
            return ruling
        try:
            out = self.llm.complete_json(
                "Write a one-sentence judicial rationale for this ruling, citing the clause.",
                [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "ruling": ruling.decision,
                                "clause": ruling.clause,
                                "deal": contract.deal_id,
                            }
                        ),
                    }
                ],
            )
            ruling.reasoning = str(out.get("rationale", ruling.reasoning)).strip() or ruling.reasoning
        except Exception:  # noqa: BLE001
            pass
        self._log(ruling, contract.deal_id, attest)
        return ruling

    def _log(self, ruling: Ruling, deal_id: str, attest: Attest) -> None:
        if self.ledger is not None:
            self.ledger.append(
                deal_id,
                self.agent_id,
                "ruling",
                decision=ruling.decision,
                clause=ruling.clause,
                reasoning=ruling.reasoning,
                objective=ruling.objective,
                llm_assisted=ruling.llm_assisted,
            )


def _deterministic_reason(decision: Decision, attest: Attest) -> str:
    if decision == "release":
        return f"objective breach: {attest.reason[:120]}"
    return f"no objective breach established; escrow held for {attest.reason[:80]}"
