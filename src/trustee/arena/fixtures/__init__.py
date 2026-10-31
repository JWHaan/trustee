"""Demo fixtures: the goods store, scenarios, transcripts, and mandates.

`manifest.json` + `goods/` are what `ArtifactStore` reads. `scenarios.json`
is what the runner reads. `mandates.json` is the spend authorization the
Trustee gates against — kept as data so the gate is inspectable, not buried
in a constructor default.

Mandates load unsigned: the runner signs them per run with a fresh principal
key (see `DealRunner._run_mandates`), so no private key is ever stored here.

Regenerate the goods with `python -m trustee.arena.fixtures.build_goods`.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ...mandate import Mandate

ROOT = Path(__file__).parent
GOODS = ROOT / "goods"
TRANSCRIPTS = ROOT / "transcripts"


def load_mandates() -> dict[str, Mandate]:
    raw = json.loads((ROOT / "mandates.json").read_text(encoding="utf-8"))
    out: dict[str, Mandate] = {}
    for m in raw["mandates"]:
        valid_until = datetime.fromisoformat(m["valid_until"]) if m.get("valid_until") else None
        out[m["agent_id"]] = Mandate(
            agent_id=m["agent_id"],
            principal=m["principal"],
            max_amount=Decimal(str(m["max_amount"])),
            allowed_categories=frozenset(m["allowed_categories"]) if m.get("allowed_categories") else None,
            allowed_counterparties=frozenset(m["allowed_counterparties"]) if m.get("allowed_counterparties") else None,
            valid_until=valid_until,
            grants=m.get("grants", "spend"),
            notes=m.get("notes", ""),
            max_total=Decimal(str(m["max_total"])) if m.get("max_total") else None,
        )
    return out


__all__ = ["ROOT", "GOODS", "TRANSCRIPTS", "load_mandates"]
