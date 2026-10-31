"""Intent mandates — consent given once, enforced on every purchase (AP2 "human-not-present").

A human who wants an agent to buy something signs ONE document: an *intent mandate*. It holds the human's own words
(`instruction`), the machine-checkable limits derived from them, and a reference to the payment method they vaulted
at PayPal (`funding_ref`). After that the agent can pay with no per-transaction click, but every purchase still has
to pass the rules below, each of them deterministic and each of them written to the ledger as a `policy` row.

Why a sibling of `Mandate` and not a subclass. A `Mandate` is a per-agent spend ceiling with replay protection by
version. An intent is a different statement: it is about *this* purchase intent (item, seller, price, count, window),
it names a funding source, and it must carry the human's words so a dispute can show what was actually asked for.
Merging the two would force `MandateEngine` to learn about items and funding. They compose instead: the classic
mandate still gates every deal first, the intent can only narrow what happens inside it.

AP2 vocabulary adopted (Agent Payments Protocol, Intent Mandate):

    AP2                                  here
    natural_language_description         instruction (verbatim, signed)
    merchants                            allowed_sellers
    skus                                 good_ids
    intent_expiry                        expires_at
    user_cart_confirmation_required      human_present (always false: the human is not present at purchase time)
    requires_refundability               require_escrow (always true: authorize/capture/void, funds held until delivery)

What the LLM may and may not do. `draft_intent` lets a model PROPOSE structured limits from the human's sentence.
The proposal is validated by code (an amount must appear in the sentence, a seller must be one the caller knows),
returned unsigned, and only the principal's key turns it into authority. Nothing here lets a model sign, widen or
override a signed intent.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from .identity import AgentKey, AgentRegistry, PrincipalRegistry, canonical_json
from .protocol import Contract, GoodSpec, new_id

log = logging.getLogger(__name__)

INTENT_VERSION = 1


def _sorted_unique(values: list[str] | None) -> list[str] | None:
    return None if values is None else sorted({str(v) for v in values})


class IntentMandate(BaseModel):
    """A principal-signed purchase intent. Frozen in spirit: every change is a new document and a new digest."""

    intent_id: str = Field(default_factory=lambda: new_id("intent"), description="Unique id of this signed intent.")
    version: int = Field(default=INTENT_VERSION, description="Format version of this document.")
    agent_id: str = Field(description="The buyer agent this intent authorizes.", examples=["buyer-agent"])
    principal: str = Field(description="The human or company that signs.", examples=["acme-procurement"])
    instruction: str = Field(
        min_length=1, max_length=2000, description="The principal's own words, verbatim. Signed with the rest."
    )
    funding_ref: str = Field(
        description="Opaque reference to the principal's vaulted PayPal funding source. Never the vault id itself."
    )
    currency: str = "USD"
    max_price: Decimal = Field(gt=0, description="Largest price of a single purchase.", examples=["175.00"])
    max_total: Decimal | None = Field(default=None, gt=0, description="Most this intent may spend in total.")
    allowed_sellers: list[str] | None = Field(default=None, description="Seller agent ids. Null means any.")
    allowed_categories: list[str] | None = Field(default=None, description="Good categories. Null means any.")
    good_ids: list[str] | None = Field(default=None, description="Exact good ids that may be bought. Null means any.")
    keywords: list[str] | None = Field(
        default=None, description="Every keyword must appear in the good's description (case-insensitive)."
    )
    required_criteria: list[str] = Field(
        default_factory=list,
        description=(
            "Each entry must be present in the contract: `sha256` means the contract commits to a deliverable hash; "
            "anything else must appear in one of the acceptance criteria."
        ),
    )
    max_purchases: int | None = Field(default=None, ge=1, description="Most purchases per `window_seconds`.")
    window_seconds: int | None = Field(default=None, ge=1, description="Length of the velocity window.")
    issued_at: datetime = Field(default_factory=datetime.now, description="When the principal signed.")
    expires_at: datetime | None = Field(default=None, description="After this instant the intent buys nothing.")
    require_escrow: bool = Field(default=True, description="Always true: money moves only after verified delivery.")
    human_present: bool = Field(
        default=False, description="Always false: this intent is for purchases made unattended."
    )
    principal_signature: str | None = Field(default=None, description="Principal's Ed25519 signature, urlsafe-base64.")

    @field_validator("allowed_sellers", "allowed_categories", "good_ids", mode="after")
    @classmethod
    def _norm_lists(cls, value: list[str] | None) -> list[str] | None:
        return _sorted_unique(value)

    @field_validator("keywords", mode="after")
    @classmethod
    def _norm_keywords(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else sorted({v.strip().lower() for v in value if v.strip()})

    @field_validator("required_criteria", mode="after")
    @classmethod
    def _norm_criteria(cls, value: list[str]) -> list[str]:
        return sorted({v.strip().lower() for v in value if v.strip()})

    @model_validator(mode="after")
    def _window_pair(self) -> IntentMandate:
        if (self.max_purchases is None) != (self.window_seconds is None):
            raise ValueError("max_purchases and window_seconds go together")
        return self

    def signing_bytes(self) -> bytes:
        """Canonical bytes of every field except the signature itself."""
        return canonical_json(self.model_dump(mode="json", exclude={"principal_signature"}))

    def digest(self) -> str:
        """sha256 hex of the signed terms: what receipts and contracts refer to."""
        return hashlib.sha256(self.signing_bytes()).hexdigest()

    def sign(self, key: AgentKey) -> IntentMandate:
        """A copy signed by the principal's key; the original is untouched."""
        return self.model_copy(update={"principal_signature": key.sign(self.signing_bytes())})

    def public(self) -> dict[str, Any]:
        """JSON-safe dict of the whole document (nothing in it is secret: `funding_ref` is only a handle)."""
        out: dict[str, Any] = self.model_dump(mode="json")
        return out


# ---------------------------------------------------------------------------------------------- the rule engine


@dataclass(frozen=True)
class RuleResult:
    """One policy check: which rule, did it pass, what was seen against what was allowed.

    `hard` marks a violation that must refuse the deal outright; a failed soft rule hands the purchase to a human."""

    rule: str
    ok: bool
    observed: str
    allowed: str
    hard: bool = False
    detail: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "result": "pass" if self.ok else "fail",
            "ok": self.ok,
            "severity": "hard" if self.hard else "soft",
            "observed": self.observed,
            "allowed": self.allowed,
            "note": self.detail or f"{self.rule}: {'pass' if self.ok else 'FAIL'} ({self.observed} vs {self.allowed})",
        }


Outcome = Literal["autonomous", "step_up", "reject"]


@dataclass
class IntentDecision:
    outcome: Outcome
    rules: list[RuleResult] = field(default_factory=list)

    @property
    def failed(self) -> list[RuleResult]:
        return [r for r in self.rules if not r.ok]

    @property
    def hard_failures(self) -> list[RuleResult]:
        return [r for r in self.rules if not r.ok and r.hard]

    def summarize(self) -> str:
        bad = self.failed
        if not bad:
            return "intent mandate satisfied"
        return "; ".join(f"{r.rule}: {r.observed} vs {r.allowed}" for r in bad)


def _now_like(moment: datetime, now: datetime) -> datetime:
    """`now` in the same awareness as `moment`, so naive and aware datetimes never meet in a comparison."""
    if moment.tzinfo is not None and now.tzinfo is None:
        return now.astimezone(UTC)
    if moment.tzinfo is None and now.tzinfo is not None:
        return now.replace(tzinfo=None)
    return now


def evaluate_intent(
    intent: IntentMandate,
    contract: Contract,
    *,
    registry: AgentRegistry | None,
    principals: PrincipalRegistry | None,
    revoked: bool,
    committed: Decimal,
    recent_purchases: int,
    funding_active: bool,
    now: datetime | None = None,
    check_funding: bool = True,
) -> IntentDecision:
    """Run every rule against one contract. Pure and deterministic: no I/O, no model, no clock but `now`.

    `committed` is what the buyer has already spent or reserved (this deal excluded) and `recent_purchases` the number
    of its purchases inside the velocity window (this deal excluded). Outcome: `reject` if a hard rule failed,
    `step_up` if only soft rules failed, `autonomous` if all passed."""
    now = now or datetime.now()
    rules: list[RuleResult] = []
    digest = contract.digest().encode("ascii")

    # -- who said what: principal -> intent, principal -> agent, agent -> contract ------------------------------
    signed = bool(
        principals is not None
        and intent.principal_signature
        and principals.verify(intent.principal, intent.principal_signature, intent.signing_bytes())
    )
    rules.append(
        RuleResult(
            "signature.principal_intent",
            signed,
            "valid" if signed else "missing or invalid",
            f"signed by principal {intent.principal}",
            hard=True,
        )
    )
    record = registry.get(contract.buyer) if registry is not None else None
    vouched = record is not None and record.principal == intent.principal and "buyer" in record.roles
    bound = contract.buyer == intent.agent_id
    rules.append(
        RuleResult(
            "signature.principal_agent",
            vouched and bound,
            f"agent {contract.buyer} under {record.principal if record else 'nobody'}",
            f"agent {intent.agent_id} vouched for by {intent.principal}",
            hard=True,
        )
    )
    party = bool(
        registry is not None and registry.verify(contract.buyer, contract.signatures.get(contract.buyer), digest)
    )
    rules.append(
        RuleResult(
            "signature.agent_contract",
            party,
            "valid" if party else "missing or invalid",
            f"contract {contract.digest()[:12]} signed by {contract.buyer}",
            hard=True,
        )
    )

    # -- the intent itself is still alive --------------------------------------------------------------------------
    rules.append(RuleResult("intent.not_revoked", not revoked, "revoked" if revoked else "active", "active", hard=True))
    expired = intent.expires_at is not None and _now_like(intent.expires_at, now) > intent.expires_at
    rules.append(
        RuleResult(
            "intent.not_expired",
            not expired,
            now.isoformat(timespec="seconds"),
            f"before {intent.expires_at.isoformat(timespec='seconds')}" if intent.expires_at else "no expiry",
            hard=True,
        )
    )
    rules.append(
        RuleResult(
            "intent.require_escrow",
            intent.require_escrow and not intent.human_present,
            f"require_escrow={intent.require_escrow} human_present={intent.human_present}",
            "require_escrow=True human_present=False",
            hard=True,
        )
    )

    # -- what is being bought, from whom, for how much ---------------------------------------------------------------
    sellers_ok = intent.allowed_sellers is None or contract.seller in intent.allowed_sellers
    rules.append(
        RuleResult(
            "seller.allowed",
            sellers_ok,
            contract.seller,
            ", ".join(intent.allowed_sellers) if intent.allowed_sellers is not None else "any seller",
            hard=True,
        )
    )
    rules.append(_item_rule(intent, contract.good))
    price_ok = contract.price <= intent.max_price
    rules.append(RuleResult("price.max_price", price_ok, f"{contract.price}", f"<= {intent.max_price}"))
    if intent.max_total is not None:
        total = committed + contract.price
        rules.append(RuleResult("budget.max_total", total <= intent.max_total, f"{total}", f"<= {intent.max_total}"))
    else:
        rules.append(RuleResult("budget.max_total", True, f"{committed + contract.price}", "no total cap"))
    if intent.max_purchases is not None and intent.window_seconds is not None:
        rules.append(
            RuleResult(
                "velocity.window",
                recent_purchases + 1 <= intent.max_purchases,
                f"{recent_purchases + 1} in {intent.window_seconds}s",
                f"<= {intent.max_purchases}",
            )
        )
    else:
        rules.append(RuleResult("velocity.window", True, f"{recent_purchases + 1}", "no limit"))
    rules.append(_criteria_rule(intent, contract.good))
    rules.append(RuleResult("currency.match", contract.currency == intent.currency, contract.currency, intent.currency))
    if check_funding:
        rules.append(
            RuleResult(
                "funding.active",
                funding_active,
                "active" if funding_active else "missing, revoked or not vaulted",
                "an active vaulted funding source of the principal",
            )
        )

    if any(not r.ok and r.hard for r in rules):
        return IntentDecision("reject", rules)
    if any(not r.ok for r in rules):
        return IntentDecision("step_up", rules)
    return IntentDecision("autonomous", rules)


def _item_rule(intent: IntentMandate, good: GoodSpec) -> RuleResult:
    problems: list[str] = []
    if intent.allowed_categories is not None and good.category not in intent.allowed_categories:
        problems.append(f"category {good.category}")
    if intent.good_ids is not None and good.good_id not in intent.good_ids:
        problems.append(f"good {good.good_id}")
    if intent.keywords:
        text = good.description.lower()
        missing = [k for k in intent.keywords if k not in text]
        if missing:
            problems.append("missing keywords " + ",".join(missing))
    allowed = []
    if intent.allowed_categories is not None:
        allowed.append("category in " + ",".join(intent.allowed_categories))
    if intent.good_ids is not None:
        allowed.append("good in " + ",".join(intent.good_ids))
    if intent.keywords:
        allowed.append("description has " + ",".join(intent.keywords))
    return RuleResult(
        "item.match",
        not problems,
        "; ".join(problems) if problems else f"{good.good_id} ({good.category})",
        "; ".join(allowed) or "any item",
    )


def _criteria_rule(intent: IntentMandate, good: GoodSpec) -> RuleResult:
    missing: list[str] = []
    for need in intent.required_criteria:
        if need == "sha256":
            if good.sha256 is None:
                missing.append("sha256")
        elif not any(need in clause.lower() for clause in good.acceptance_criteria):
            missing.append(need)
    return RuleResult(
        "criteria.required",
        not missing,
        "missing " + ",".join(missing) if missing else "all present",
        ",".join(intent.required_criteria) or "none required",
    )


# ------------------------------------------------------------------------------------------------ state around it


class FundingResolver(Protocol):
    """Resolves a funding_ref to a usable vault id. Implemented by `funding.FundingRegistry`; the id stays inside."""

    def is_active(self, principal: str, funding_ref: str) -> bool: ...

    def vault_id(self, principal: str, funding_ref: str) -> str | None: ...


class IntentBook:
    """Intent mandates by buyer agent, revocations, and the purchase log that feeds the velocity rule."""

    def __init__(self) -> None:
        self._intents: dict[str, IntentMandate] = {}
        self._revoked: set[str] = set()  # agent ids whose CURRENT intent is revoked
        self._revoked_digests: set[str] = set()
        self._purchases: dict[str, tuple[str, datetime]] = {}  # deal id -> (agent, when)
        self._lock = threading.RLock()

    def put(self, intent: IntentMandate, revoked: bool = False) -> None:
        with self._lock:
            self._intents[intent.agent_id] = intent
            if revoked:
                self._revoked.add(intent.agent_id)
                self._revoked_digests.add(intent.digest())
            else:
                self._revoked.discard(intent.agent_id)

    def get(self, agent_id: str) -> IntentMandate | None:
        with self._lock:
            return self._intents.get(agent_id)

    def is_revoked(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._revoked

    def digest_revoked(self, digest: str) -> bool:
        with self._lock:
            return digest in self._revoked_digests

    def revoke(self, agent_id: str) -> bool:
        with self._lock:
            intent = self._intents.get(agent_id)
            if intent is None:
                return False
            self._revoked.add(agent_id)
            self._revoked_digests.add(intent.digest())
            return True

    def note_purchase(self, deal_id: str, agent_id: str, when: datetime) -> None:
        with self._lock:
            self._purchases.setdefault(deal_id, (agent_id, when))

    def drop_purchase(self, deal_id: str) -> None:
        with self._lock:
            self._purchases.pop(deal_id, None)

    def recent(self, agent_id: str, window: timedelta | None, now: datetime, exclude_deal: str | None = None) -> int:
        with self._lock:
            n = 0
            for deal_id, (who, when) in self._purchases.items():
                if who != agent_id or deal_id == exclude_deal:
                    continue
                if window is None or _now_like(when, now) - when <= window:
                    n += 1
            return n


@dataclass
class Autonomy:
    """Everything the Trustee needs to pay without a human: the intents, the funding vault, the key registries."""

    book: IntentBook
    funding: FundingResolver
    principals: PrincipalRegistry | None = None


# ------------------------------------------------------------------------------------------------ drafting from words


class DraftError(ValueError):
    """The sentence cannot be turned into a safe intent (no amount, unknown seller, ...). Nothing is signed."""


@dataclass
class IntentDraft:
    """An unsigned intent plus how it was derived. It is a proposal: it carries no authority until signed."""

    intent: IntentMandate
    source: Literal["llm", "rules"]
    notes: list[str] = field(default_factory=list)

    def sign(self, key: AgentKey) -> IntentMandate:
        return self.intent.sign(key)


_AMOUNT = re.compile(
    r"\$\s*(\d{1,7}(?:,\d{3})*(?:\.\d{1,2})?)|(\d{1,7}(?:\.\d{1,2})?)\s*(?:usd|dollars?)", re.IGNORECASE
)
_CAP_WORDS = re.compile(
    r"(?:at most|up to|no more than|not more than|max(?:imum)?(?: of)?|under|below|within|cap(?:ped)? at|limit(?: of)?)"
    r"\s*\$?\s*(\d{1,7}(?:,\d{3})*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def _amounts_in(text: str) -> list[Decimal]:
    found: list[Decimal] = []
    for m in _AMOUNT.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        try:
            found.append(Decimal(raw))
        except InvalidOperation:  # pragma: no cover - the regex only yields digits
            continue
    return found


def _money(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value).replace(",", "").lstrip("$"))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def draft_intent(
    instruction: str,
    llm: Any | None,
    *,
    agent_id: str,
    principal: str,
    funding_ref: str,
    good: GoodSpec | None = None,
    known_sellers: Mapping[str, str] | None = None,
    now: datetime | None = None,
    default_ttl: timedelta = timedelta(days=7),
) -> IntentDraft:
    """Propose an unsigned intent mandate from the human's sentence.

    `known_sellers` maps a name a human would say ("Tidewater") to the seller's agent id; `good` is what the caller's
    agent is configured to buy. The model (if any) may suggest numbers and names; this function keeps a suggestion only
    when code can tie it to the sentence: a price cap must be an amount written in the instruction, a seller must be one
    in `known_sellers`. The instruction is stored exactly as given, the human-present and escrow flags are forced, and
    the funding reference is the caller's. The result is a draft: show it to the principal, who signs or discards it.
    """
    text = instruction.strip()
    if not text:
        raise DraftError("the instruction is empty")
    now = now or datetime.now()
    notes: list[str] = []
    proposal: dict[str, Any] = {}
    source: Literal["llm", "rules"] = "rules"
    if llm is not None:
        try:
            proposal = llm.complete_json(
                "You turn a person's purchase instruction into JSON limits for a payment policy engine. "
                "Reply with ONLY an object: max_price (string), max_total (string or null), sellers (list of names "
                "from the instruction), max_purchases (int or null), window_hours (int or null), "
                "expires_in_hours (int or null), require_sha256 (bool). Use only what the instruction says.",
                [{"role": "user", "content": text}],
            )
            if not isinstance(proposal, dict):
                proposal = {}
        except Exception as exc:  # noqa: BLE001 - a failed model never blocks drafting; the rules path still works
            log.info("intent drafting model unavailable: %s", exc)
            proposal = {}
    amounts = _amounts_in(text)

    def stated(value: Any) -> Decimal | None:
        """A model-proposed amount, kept only if the sentence contains that number."""
        d = _money(value)
        if d is not None and d in amounts:
            return d
        if d is not None:
            notes.append(f"ignored model amount {d}: not written in the instruction")
        return None

    max_price = stated(proposal.get("max_price"))
    if max_price is not None:
        source = "llm"
    if max_price is None:
        capped = _CAP_WORDS.search(text)
        max_price = _money(capped.group(1)) if capped else (max(amounts) if amounts else None)
    if max_price is None:
        raise DraftError("no spending limit found: write an amount such as 'at most $175'")
    max_total = stated(proposal.get("max_total"))
    if max_total is not None and max_total < max_price:
        max_total = None

    sellers: list[str] | None = None
    names = [str(n) for n in (proposal.get("sellers") or []) if isinstance(n, str)]
    lowered = text.lower()
    if known_sellers:
        mentioned = {
            agent
            for name, agent in known_sellers.items()
            if name.lower() in lowered or name.lower() in {n.lower() for n in names}
        }
        if mentioned:
            sellers = sorted(mentioned)
    if re.search(r"only (?:from|with|buy from)", lowered) and sellers is None:
        raise DraftError("the instruction restricts the seller but no known seller matches")

    required = (
        ["sha256"] if (proposal.get("require_sha256") is True or re.search(r"sha-?256|checksum|hash", lowered)) else []
    )
    max_purchases = proposal.get("max_purchases")
    window_hours = proposal.get("window_hours")
    window_seconds: int | None = None
    count: int | None = None
    if isinstance(max_purchases, int) and isinstance(window_hours, int) and 1 <= max_purchases and 1 <= window_hours:
        count, window_seconds = max_purchases, window_hours * 3600
    elif re.search(r"\b(?:one|a single|1)\b[^.;]*\b(?:purchase|time|order)\b|\bonce\b", lowered):
        count, window_seconds = 1, int(default_ttl.total_seconds())
    hours = proposal.get("expires_in_hours")
    ttl = timedelta(hours=hours) if isinstance(hours, int) and 1 <= hours <= 24 * 365 else default_ttl

    intent = IntentMandate(
        agent_id=agent_id,
        principal=principal,
        instruction=text,
        funding_ref=funding_ref,
        currency="USD",
        max_price=max_price,
        max_total=max_total,
        allowed_sellers=sellers,
        allowed_categories=[good.category] if good else None,
        good_ids=[good.good_id] if good else None,
        required_criteria=required,
        max_purchases=count,
        window_seconds=window_seconds,
        issued_at=now,
        expires_at=now + ttl,
    )
    if source == "rules":
        notes.append("limits read from the sentence by rules; no model proposal was usable")
    return IntentDraft(intent, source, notes)


def intent_summary(intent: IntentMandate) -> str:
    """One line a human can check against what they said."""
    parts = [f"up to {intent.max_price} {intent.currency} per purchase"]
    if intent.allowed_sellers is not None:
        parts.append("only from " + ", ".join(intent.allowed_sellers))
    if intent.good_ids is not None:
        parts.append("item " + ", ".join(intent.good_ids))
    if intent.required_criteria:
        parts.append("must commit to " + ", ".join(intent.required_criteria))
    if intent.max_total is not None:
        parts.append(f"{intent.max_total} in total")
    if intent.max_purchases is not None and intent.window_seconds is not None:
        parts.append(f"{intent.max_purchases} per {intent.window_seconds // 3600}h")
    if intent.expires_at is not None:
        parts.append("until " + intent.expires_at.isoformat(timespec="minutes"))
    return "; ".join(parts)
