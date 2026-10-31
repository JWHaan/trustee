"""Trustee's A2A skills: one per `DealService` operation, with the same payloads as the REST API.

A skill is invoked with a `SendMessage` whose message carries one DataPart:

    {"skill": "open-escrow", "input": { ...the body you would POST to /v1/deals... }}

(`message.metadata.skill` is accepted as an alternative to the `skill` key; the DataPart is then the input.)
The input is validated with the same pydantic models the REST routes use (`wire.py`, `protocol.py`), and the
call goes to the same `DealService` method. A2A is only the transport: the Ed25519 signatures inside the
payloads are what authorize anything, exactly as over REST. Nothing in this module can move money by itself;
it forwards a signed message to the service, which runs the mandate gate, the identity check and the state
machine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ..intent import IntentMandate
from ..platform.service import DealService, ServiceError
from ..platform.wire import AgentIn, FundingSetupIn, MandateIn, PrincipalIn, mandate_from_in
from ..protocol import Attest, Contract, Delivery
from .models import Message, Role, data_part, invalid_params, invalid_params_from, new_id, text_part


class RevokeSkillIn(BaseModel):
    agent_id: str
    signature: str


class RegisterFundingIn(FundingSetupIn):
    principal: str


class RevokeIntentIn(BaseModel):
    agent_id: str
    signature: str


class GetDealIn(BaseModel):
    deal_id: str


@dataclass(frozen=True)
class Skill:
    id: str
    summary: str
    model: type[BaseModel]
    shape: str  # one line a human or an LLM can read to build the input
    run: Callable[[DealService, Any, str | None], dict[str, Any]]
    deal_id: Callable[[Any], str] | None = None  # set for skills that act on one deal

    @property
    def deal_bound(self) -> bool:
        return self.deal_id is not None


@dataclass
class SkillOutcome:
    """What a skill did. Exactly one of `data` and `error` is set."""

    skill: str
    deal_id: str | None = None
    data: dict[str, Any] | None = None
    error: ServiceError | None = None


# -------------------------------------------------------------------------------------------- runners


def _register_principal(svc: DealService, body: PrincipalIn, auth: str | None) -> dict[str, Any]:
    svc.check_registration_token(auth)  # the HTTP Authorization header of the /a2a request
    result, created = svc.register_principal(body.principal, body.public_key, body.proof)
    return {"created": created, **result}


def _register_agent(svc: DealService, body: AgentIn, auth: str | None) -> dict[str, Any]:
    result, created = svc.register_agent(
        body.agent_id,
        body.public_key,
        body.principal,
        body.roles,
        body.paypal_email,
        body.proof,
        body.principal_signature,
    )
    return {"created": created, **result}


def _publish_mandate(svc: DealService, body: MandateIn, auth: str | None) -> dict[str, Any]:
    return svc.publish_mandate(mandate_from_in(body))


def _revoke_mandate(svc: DealService, body: RevokeSkillIn, auth: str | None) -> dict[str, Any]:
    return svc.revoke_mandate(body.agent_id, body.signature)


def _publish_intent(svc: DealService, body: IntentMandate, auth: str | None) -> dict[str, Any]:
    return svc.publish_intent(body)


def _revoke_intent(svc: DealService, body: RevokeIntentIn, auth: str | None) -> dict[str, Any]:
    return svc.revoke_intent(body.agent_id, body.signature)


def _register_funding(svc: DealService, body: RegisterFundingIn, auth: str | None) -> dict[str, Any]:
    return svc.begin_funding(body.principal, body.nonce, body.signature, body.funding_ref)


def _open_escrow(svc: DealService, body: Contract, auth: str | None) -> dict[str, Any]:
    _, created = svc.open_deal(body)
    return {"created": created}


def _record_delivery(svc: DealService, body: Delivery, auth: str | None) -> dict[str, Any]:
    svc.on_delivery(str(body.deal_id), body)
    return {}


def _attest_delivery(svc: DealService, body: Attest, auth: str | None) -> dict[str, Any]:
    svc.on_attest(str(body.deal_id), body)
    return {}


def _get_deal(svc: DealService, body: GetDealIn, auth: str | None) -> dict[str, Any]:
    svc.view(body.deal_id)  # raises 404 for an unknown deal
    return {}


SKILLS: dict[str, Skill] = {
    s.id: s
    for s in (
        Skill(
            "register-principal",
            "Register the human or company that owns agents (REST: POST /v1/principals).",
            PrincipalIn,
            '{"principal": str, "public_key": b64, "proof": b64 signature over {principal, public_key}}',
            _register_principal,
        ),
        Skill(
            "register-agent",
            "Bind an agent key to an agent id under a principal that vouches for it (REST: POST /v1/agents).",
            AgentIn,
            '{"agent_id", "public_key", "principal", "roles": ["buyer"|"seller"], "paypal_email"?, '
            '"proof": agent signature, "principal_signature": principal signature}',
            _register_agent,
        ),
        Skill(
            "publish-mandate",
            "Publish a principal-signed spend or receive mandate for an agent (REST: POST /v1/mandates).",
            MandateIn,
            '{"agent_id", "principal", "max_amount", "grants": "spend"|"receive", "version", '
            '"principal_signature", ...optional limits}',
            _publish_mandate,
        ),
        Skill(
            "revoke-mandate",
            "Revoke an agent's mandate; the principal signs {revoke: agent_id} (REST: POST /v1/mandates/{id}/revoke).",
            RevokeSkillIn,
            '{"agent_id": str, "signature": b64 principal signature over {"revoke": agent_id}}',
            _revoke_mandate,
        ),
        Skill(
            "register-funding",
            "Start the one-time PayPal consent that vaults the principal's payment method; returns a funding_ref "
            "and the link to open once (REST: POST /v1/principals/{principal}/funding).",
            RegisterFundingIn,
            '{"principal": str, "nonce": str, "funding_ref"?: str, "signature": b64 principal signature over '
            '{"action": "funding_setup", "principal", "nonce", "funding_ref"}}',
            _register_funding,
        ),
        Skill(
            "publish-intent",
            "Publish a principal-signed intent mandate: the human's instruction and the limits for paying "
            "without per-purchase approval (REST: POST /v1/intents).",
            IntentMandate,
            '{"agent_id", "principal", "instruction", "funding_ref", "max_price", "allowed_sellers"?, "good_ids"?, '
            '"required_criteria"?, "max_total"?, "max_purchases"?, "window_seconds"?, "expires_at"?, '
            '"principal_signature"}',
            _publish_intent,
        ),
        Skill(
            "revoke-intent",
            "Revoke an agent's intent mandate; the principal signs {revoke_intent: agent_id} "
            "(REST: POST /v1/intents/{agent_id}/revoke).",
            RevokeIntentIn,
            '{"agent_id": str, "signature": b64 principal signature over {"revoke_intent": agent_id}}',
            _revoke_intent,
        ),
        Skill(
            "open-escrow",
            "Open an escrow from a contract signed by buyer and seller. Returns the deal task "
            "(taskId = deal_id). Idempotent on deal_id (REST: POST /v1/deals).",
            Contract,
            '{"deal_id", "buyer", "seller", "good": {...}, "price", "currency", "deadline", "formed_at", '
            '"signatures": {buyer_id: sig, seller_id: sig}}',
            _open_escrow,
            lambda c: c.deal_id,
        ),
        Skill(
            "record-delivery",
            "Seller reports the deliverable and its sha256, signed (REST: POST /v1/deals/{id}/delivery).",
            Delivery,
            '{"sender": seller_id, "deal_id", "deliverable_ref", "content_hash", "signature"}',
            _record_delivery,
            lambda d: str(d.deal_id),
        ),
        Skill(
            "attest-delivery",
            "Buyer accepts (funds captured to the seller) or rejects (arbitration), signed "
            "(REST: POST /v1/deals/{id}/attest).",
            Attest,
            '{"sender": buyer_id, "deal_id", "verdict": "accept"|"reject", "reason", "evidence": {...}, "signature"}',
            _attest_delivery,
            lambda a: str(a.deal_id),
        ),
        Skill(
            "get-deal",
            "Current task for a deal (same as GetTask) (REST: GET /v1/deals/{id}).",
            GetDealIn,
            '{"deal_id": str}',
            _get_deal,
            lambda g: g.deal_id,
        ),
    )
}


@dataclass
class Prepared:
    """A skill invocation whose input has been validated but not yet run."""

    skill: Skill
    model: Any
    deal_id: str | None


def prepare_skill(skill_id: str, payload: Any) -> Prepared:
    """Validate `payload` against the skill's model. Nothing is executed.

    Raises A2AError (-32602) for an unknown skill or a payload that does not fit the model: those are
    malformed requests, not refusals. Splitting this from `execute_skill` lets the server check the
    addressing (taskId / contextId against the deal in the payload) before anything runs."""
    skill = SKILLS.get(skill_id)
    if skill is None:
        known = ", ".join(sorted(SKILLS))
        raise invalid_params(f"unknown skill {skill_id[:60]!r}; known skills: {known}")
    if not isinstance(payload, dict):
        raise invalid_params("the skill input must be a JSON object")
    try:
        model = skill.model.model_validate(payload)
    except ValidationError as exc:
        raise invalid_params_from(exc, "input.") from None
    return Prepared(skill, model, skill.deal_id(model) if skill.deal_id is not None else None)


def execute_skill(service: DealService, prepared: Prepared, authorization: str | None) -> SkillOutcome:
    """Run a prepared skill. Service errors (401/403/404/409/422/502) come back in the outcome."""
    skill = prepared.skill
    try:
        data = skill.run(service, prepared.model, authorization)
    except ServiceError as exc:
        return SkillOutcome(skill.id, prepared.deal_id, error=exc)
    return SkillOutcome(skill.id, prepared.deal_id, data=data)


def help_message(context_id: str | None = None) -> Message:
    """Reply to a message with no skill invocation: what Trustee does and the shape to send."""
    lines = [
        "Trustee holds a buyer's PayPal funds in escrow until the buyer attests that the seller's delivery matches "
        "the contract, then captures them to the seller or voids them. I act on signed messages, not on chat.",
        "",
        'To use a skill, send a message with one DataPart: {"skill": "<id>", "input": {...}}. Skills:',
        *(f"- {s.id}: {s.summary} input: {s.shape}" for s in SKILLS.values()),
        "",
        "Every contract, delivery and attestation must carry the Ed25519 signature of its party (see the Agent "
        "Card for the exact signing bytes). A deal is a task whose id is the deal_id.",
    ]
    catalog = {s.id: {"summary": s.summary, "input": s.shape} for s in SKILLS.values()}
    return Message(
        messageId=new_id(),
        role=Role.AGENT,
        parts=[text_part("\n".join(lines)), data_part({"skills": catalog})],
        contextId=context_id,
    )
