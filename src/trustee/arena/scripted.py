"""ScriptedLLM — the reliability net.

It answers exactly the two calls the agents make, and nothing else:

  * `complete(system, ...)` — negotiation moves. The speaker is inferred from
    the system prompt (`Negotiator` says "negotiator for buyer"/"... for
    seller"; `SellerAgent` says "You are the seller in a price negotiation"),
    so no agent code has to know it is being scripted.
  * `complete_json(system, ...)` — narration. Returns `{}` on purpose: every
    caller in the codebase falls back to its own deterministic sentence
    (`fact`, `joined`, `ruling.reasoning`). Inventing text here would put
    words in the agents' mouths and make the tape fiction.

An exhausted queue raises ValueError, which is the failure mode the agents
already handle: `Negotiator.respond` retries then cap-accepts, and
`SellerAgent._propose_counter` falls back to a floor counter.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm import LLM


@dataclass(frozen=True)
class Move:
    speaker: str  # "buyer" | "seller"
    kind: str  # "offer" | "counter" | "accept"
    price: str
    note: str = ""


@dataclass
class ScriptedLLM(LLM):
    """Replays recorded moves. Same interface as `LLM`, zero network."""

    moves: list[Move] = field(default_factory=list)
    _queues: dict[str, deque[Move]] = field(default_factory=lambda: defaultdict(deque), init=False)
    consumed: list[Move] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # Deliberately not calling LLM.__init__: it would build an OpenAI
        # client, and scripted mode must work with no key, no network, no SDK.
        self.model = "scripted"
        self.last_raw = ""
        for move in self.moves:
            self._queues[move.speaker].append(move)

    # ------------------------------------------------------------------ LLM API

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:  # type: ignore[override]
        speaker = _speaker_of(system)
        if speaker is None:
            # A prompt we did not record. Better an empty JSON object than a
            # crash: narration callers all tolerate it.
            return "{}"
        queue = self._queues[speaker]
        if not queue:
            raise ValueError(f"scripted transcript exhausted for {speaker}")
        move = queue.popleft()
        self.consumed.append(move)
        self.last_raw = json.dumps({"kind": move.kind, "price": move.price})
        return self.last_raw

    def complete_json(self, system: str, messages: list[dict[str, str]]) -> dict[str, Any]:  # type: ignore[override]
        return {}

    # --------------------------------------------------------------- diagnostics

    @property
    def remaining(self) -> int:
        return sum(len(q) for q in self._queues.values())

    @classmethod
    def from_file(cls, path: Path) -> ScriptedLLM:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(moves=[Move(**m) for m in raw["moves"]])


def _speaker_of(system: str) -> str | None:
    s = system.lower()
    if "negotiator for buyer" in s:
        return "buyer"
    if "negotiator for seller" in s or "you are the seller" in s:
        return "seller"
    return None
