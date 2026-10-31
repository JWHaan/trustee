"""A scenario is data, not code.

The plan's claim that honest and cheat runs share one code path only holds if
the *differences* live in a file. So: prices, floors, budget, the good, and
the recorded negotiation all come from `fixtures/scenarios.json`; the runner
branches on exactly one boolean (`cheat`) and passes it to
`seller.deliver()`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from ..protocol import GoodSpec, PartialTerms

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    blurb: str
    good: GoodSpec
    ask: Decimal  # seller's opening ask
    floor: Decimal  # seller's walk-away (hard clamp in the agent)
    budget: Decimal  # buyer's mandate ceiling for this deal
    cheat: bool  # ship the corrupt twin
    transcript: str  # stem under fixtures/transcripts/
    attack: str | None = None  # e.g. "forged_signature"; None for an ordinary run
    # "negotiated" (in-process agents) | "outside" (agents driving /v1 over HTTP) | "a2a" (agents on A2A servers)
    flow: str = "negotiated"
    agreed_price: Decimal | None = None  # outside flow: the price agreed off-platform
    partial: PartialTerms | None = None  # pro-rata settlement term signed into the contract

    @property
    def opening_price(self) -> Decimal:
        """Buyer opens ~20% under the ask, never over budget (mirrors the agent)."""
        return min((self.ask * Decimal("0.8")).quantize(Decimal("0.01")), self.budget)


def _parse(raw: dict) -> Scenario:
    good = GoodSpec.model_validate(raw["good"])
    return Scenario(
        name=raw["name"],
        title=raw["title"],
        blurb=raw["blurb"],
        good=good,
        ask=Decimal(str(raw["ask"])),
        floor=Decimal(str(raw["floor"])),
        budget=Decimal(str(raw["budget"])),
        cheat=bool(raw["cheat"]),
        transcript=raw["transcript"],
        attack=raw.get("attack"),
        flow=raw.get("flow", "negotiated"),
        agreed_price=Decimal(str(raw["agreed_price"])) if raw.get("agreed_price") else None,
        partial=PartialTerms.model_validate(raw["partial"]) if raw.get("partial") else None,
    )


@lru_cache(maxsize=1)
def load_scenarios() -> dict[str, Scenario]:
    data = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
    return {raw["name"]: _parse(raw) for raw in data["scenarios"]}


def get_scenario(name: str) -> Scenario:
    try:
        return load_scenarios()[name]
    except KeyError:
        known = ", ".join(sorted(load_scenarios())) or "none"
        raise KeyError(f"unknown scenario '{name}' (known: {known})") from None
