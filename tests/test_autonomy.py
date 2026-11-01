"""Human-not-present payments: intent mandates, funding, the policy rules, hash-chained ledger, signed receipts."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trustee import verify as verify_cli
from trustee.agents.trustee import Trustee
from trustee.arena.api import create_app
from trustee.arena.rows import to_row
from trustee.arena.runner import DealRunner
from trustee.arena.scenario import get_scenario
from trustee.funding import FundingRegistry
from trustee.identity import AgentKey, AgentRecord, AgentRegistry, PrincipalRegistry
from trustee.intent import (
    Autonomy,
    DraftError,
    IntentBook,
    IntentMandate,
    draft_intent,
    evaluate_intent,
)
from trustee.ledger import Ledger, verify_chain
from trustee.mandate import Mandate, MandateEngine
from trustee.paypal import EscrowOrder, PayPalClient
from trustee.platform import DealService
from trustee.protocol import Attest, Contract, Delivery, GoodSpec
from trustee.receipts import build_receipt, load_signing_key
from trustee.state import Deal
from trustee.store import Store

SECRET_VAULT = "VAULT-SECRET-7f3a91c2d8"  # the sentinel that must never leave the store
GOOD_HASH = "a" * 64
T0 = datetime.now()
INSTRUCTION = "Buy the Q3 returns dataset; spend at most $175; only from Tidewater; must match the published sha256"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda s: None)


def good(sha: str | None = GOOD_HASH, criteria: list[str] | None = None, good_id: str = "returns") -> GoodSpec:
    return GoodSpec(
        good_id=good_id,
        description="Q3 2026 returns dataset",
        category="data",
        acceptance_criteria=criteria or ["sha256 matches contract"],
        deliverable_ref="returns.csv",
        sha256=sha,
    )


class SpyPayPal(PayPalClient):
    """Dry-run PayPal that records every money call and what vault id it was given."""

    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.calls: list[str] = []
        self.vault_ids: list[str | None] = []

    def create_and_authorize(self, **kwargs: Any) -> EscrowOrder:
        self.calls.append("authorize")
        self.vault_ids.append(kwargs.get("vault_id"))
        return super().create_and_authorize(**kwargs)

    def capture(self, order: EscrowOrder, **kwargs: Any) -> EscrowOrder:
        self.calls.append("capture")
        return super().capture(order, **kwargs)

    def void(self, order: EscrowOrder, reason: str = "", **kwargs: Any) -> EscrowOrder:
        self.calls.append("void")
        return super().void(order, reason, **kwargs)


class Rig:
    """One buyer, one seller, one principal, a vaulted funding source and a signed intent: the whole policy stack."""

    def __init__(self, paypal: PayPalClient | None = None, **intent_kw: Any) -> None:
        self.paypal = paypal or SpyPayPal()
        self.keys = {n: AgentKey.generate() for n in ("org", "buyer", "seller", "stranger")}
        self.registry = AgentRegistry()
        for agent, role in (("buyer", "buyer"), ("seller", "seller")):
            self.registry.register(AgentRecord(agent, self.keys[agent].public_key_b64, "org", frozenset({role})))
        self.principals = PrincipalRegistry()
        self.principals.register("org", self.keys["org"].public_key_b64)
        grants = {
            "buyer": Mandate("buyer", "org", Decimal("500"), grants="spend"),
            "seller": Mandate("seller", "org", Decimal("500"), grants="receive"),
        }
        self.mandates = MandateEngine(
            {k: v.sign(self.keys["org"]) for k, v in grants.items()}, principals=self.principals
        )
        self.store = Store(":memory:")
        self.funding = FundingRegistry(self.store, PayPalClient(dry_run=True))
        self.ref = self.funding.register_fake("org")
        self.book = IntentBook()
        self.ledger = Ledger()
        self.autonomy = Autonomy(self.book, self.funding, self.principals)
        self.intent = self.make_intent(**intent_kw)
        self.book.put(self.intent)

    def make_intent(self, signer: str = "org", sign: bool = True, **kw: Any) -> IntentMandate:
        fields: dict[str, Any] = {
            "agent_id": "buyer",
            "principal": "org",
            "instruction": INSTRUCTION,
            "funding_ref": self.ref,
            "max_price": Decimal("175.00"),
            "allowed_sellers": ["seller"],
            "good_ids": ["returns"],
            "required_criteria": ["sha256"],
            "issued_at": T0,
            "expires_at": T0 + timedelta(days=7),
        }
        fields.update(kw)
        intent = IntentMandate(**fields)
        return intent.sign(self.keys[signer]) if sign else intent

    def contract(
        self, deal_id: str = "deal-1", price: str = "156.00", seller: str = "seller", **good_kw: Any
    ) -> Contract:
        c = Contract(
            deal_id=deal_id,
            buyer="buyer",
            seller=seller,
            good=good(**good_kw),
            price=Decimal(price),
            deadline=T0 + timedelta(hours=1),
            formed_at=T0,
        )
        return c.sign("buyer", self.keys["buyer"]).sign("seller", self.keys["seller"])

    def trustee(self, deal_id: str = "deal-1") -> Trustee:
        deal = Deal(deal_id)
        from trustee.state import DealState

        deal.transition(DealState.NEGOTIATING, "n")
        return Trustee(
            deal, self.mandates, self.paypal, ledger=self.ledger, registry=self.registry, autonomy=self.autonomy
        )

    def rules(self, deal_id: str = "deal-1") -> dict[str, str]:
        return {
            e.payload["rule"]: e.payload["result"]
            for e in self.ledger.tail(deal_id, kinds=("policy",))
            if e.payload.get("event") == "policy_rule"
        }

    def delivery_and_attest(self, contract: Contract, verdict: str = "accept") -> tuple[Delivery, Attest]:
        d = Delivery(
            sender="seller", deal_id=contract.deal_id, deliverable_ref="returns.csv", content_hash=GOOD_HASH
        ).sign(self.keys["seller"])
        a = Attest(sender="buyer", deal_id=contract.deal_id, verdict=verdict, reason="ok").sign(self.keys["buyer"])  # type: ignore[arg-type]
        return d, a


# ----------------------------------------------------------------------------------------- intent document


def test_intent_signature_verifies_and_any_change_breaks_it() -> None:
    rig = Rig()
    i = rig.intent
    assert rig.principals.verify("org", i.principal_signature, i.signing_bytes())
    for change in (
        {"max_price": Decimal("1750.00")},
        {"instruction": "buy anything"},
        {"allowed_sellers": ["seller", "evil"]},
        {"funding_ref": "fund_other"},
        {"version": 2},
        {"expires_at": T0 + timedelta(days=700)},
        {"human_present": True},
    ):
        tampered = i.model_copy(update=change)
        assert not rig.principals.verify("org", tampered.principal_signature, tampered.signing_bytes()), change
    assert i.model_copy(update={"max_price": Decimal("1.00")}).digest() != i.digest()


def test_intent_round_trips_through_json_without_changing_its_signed_bytes() -> None:
    i = Rig().intent
    again = IntentMandate.model_validate(json.loads(json.dumps(i.public())))
    assert again.signing_bytes() == i.signing_bytes() and again.digest() == i.digest()


def test_velocity_fields_come_as_a_pair() -> None:
    with pytest.raises(ValueError):
        IntentMandate(
            agent_id="a", principal="p", instruction="x", funding_ref="f", max_price=Decimal("1"), max_purchases=2
        )


# ---------------------------------------------------------------------------------------------- drafting


def test_draft_keeps_the_instruction_and_forces_the_safe_flags() -> None:
    class Greedy:
        def complete_json(self, system: str, messages: list[dict[str, str]]) -> dict[str, Any]:
            return {
                "max_price": "9999",
                "max_total": "50000",
                "sellers": ["Tidewater", "Mallory"],
                "require_sha256": True,
            }

    draft = draft_intent(
        INSTRUCTION,
        Greedy(),
        agent_id="buyer",
        principal="org",
        funding_ref="fund_x",
        good=good(),
        known_sellers={"Tidewater": "seller"},
    )
    i = draft.intent
    assert i.instruction == INSTRUCTION  # verbatim
    assert i.max_price == Decimal("175") and i.max_total is None  # the model's 9999 is not in the sentence
    assert i.allowed_sellers == ["seller"]  # "Mallory" is not a seller the caller knows
    assert i.require_escrow and not i.human_present
    assert i.principal_signature is None  # a draft carries no authority
    assert any("9999" in n for n in draft.notes)


def test_draft_without_a_model_reads_the_sentence() -> None:
    d = draft_intent(
        INSTRUCTION, None, agent_id="b", principal="p", funding_ref="f", good=good(), known_sellers={"Tidewater": "t1"}
    )
    assert d.source == "rules" and d.intent.max_price == Decimal("175")
    assert d.intent.required_criteria == ["sha256"] and d.intent.allowed_sellers == ["t1"]


def test_draft_refuses_what_it_cannot_ground() -> None:
    with pytest.raises(DraftError):
        draft_intent("buy the dataset", None, agent_id="b", principal="p", funding_ref="f")
    with pytest.raises(DraftError):
        draft_intent(
            "buy it for at most $10, only from Acme", None, agent_id="b", principal="p", funding_ref="f",
            known_sellers={"Tidewater": "t"},
        )  # fmt: skip


def test_signing_freezes_the_draft_so_widening_is_a_forgery() -> None:
    rig = Rig()
    draft = draft_intent(INSTRUCTION, None, agent_id="buyer", principal="org", funding_ref=rig.ref, good=good(),
                         known_sellers={"Tidewater": "seller"})  # fmt: skip
    signed = draft.sign(rig.keys["org"])
    widened = signed.model_copy(update={"max_price": Decimal("5000.00")})
    assert not rig.principals.verify("org", widened.principal_signature, widened.signing_bytes())
    d = evaluate_intent(
        widened, rig.contract("d", "4000.00"), registry=rig.registry, principals=rig.principals, revoked=False,
        committed=Decimal(0), recent_purchases=0, funding_active=True, now=T0,
    )  # fmt: skip
    assert d.outcome == "reject"


# ------------------------------------------------------------------------------------------------ the rules

ALL_RULES = {
    "signature.principal_intent", "signature.principal_agent", "signature.agent_contract", "intent.not_revoked",
    "intent.not_expired", "intent.require_escrow", "seller.allowed", "item.match", "price.max_price",
    "budget.max_total", "velocity.window", "criteria.required", "currency.match", "funding.active",
}  # fmt: skip


def test_all_rules_pass_pays_autonomously_with_the_vault_id_and_one_row_per_rule() -> None:
    rig = Rig()
    paypal = rig.paypal
    assert isinstance(paypal, SpyPayPal)
    decision = rig.trustee().on_contract(rig.contract())
    assert decision.action == "escrowed"
    assert set(rig.rules()) == ALL_RULES and set(rig.rules().values()) == {"pass"}
    assert paypal.vault_ids[0] is not None and paypal.vault_ids[0].startswith("dry-vault-")
    rows = rig.ledger.tail("deal-1", kinds=("policy",))
    assert any(e.payload.get("outcome") == "autonomous" for e in rows)
    assert not any("approve_url" in e.payload for e in rig.ledger.tail("deal-1"))


@pytest.mark.parametrize(
    ("label", "intent_kw", "contract_kw", "rule"),
    [
        ("seller", {}, {"seller": "seller"}, "seller.allowed"),
        ("expired", {"expires_at": T0 - timedelta(days=1)}, {}, "intent.not_expired"),
        ("unsigned", {"sign": False}, {}, "signature.principal_intent"),
        ("forged", {"signer": "stranger"}, {}, "signature.principal_intent"),
    ],
)
def test_hard_violation_refuses_the_deal_with_zero_paypal_calls(label, intent_kw, contract_kw, rule) -> None:
    rig = Rig(**intent_kw)
    if label == "seller":
        rig.book.put(rig.make_intent(allowed_sellers=["somebody-else"]))
    decision = rig.trustee().on_contract(rig.contract(**contract_kw))
    assert decision.action == "rejected_mandate", label
    assert rig.rules()[rule] == "fail"
    assert rig.paypal.calls == []  # type: ignore[attr-defined]
    assert rule in "; ".join(decision.report.violations)  # type: ignore[union-attr]


def test_revoked_intent_is_a_hard_refusal() -> None:
    rig = Rig()
    rig.book.revoke("buyer")
    decision = rig.trustee().on_contract(rig.contract())
    assert decision.action == "rejected_mandate" and rig.rules()["intent.not_revoked"] == "fail"
    assert rig.paypal.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("intent_kw", "contract_kw", "rule"),
    [
        ({}, {"price": "190.00"}, "price.max_price"),
        ({"good_ids": ["other-good"]}, {}, "item.match"),
        ({"keywords": ["invoices"]}, {}, "item.match"),
        ({"max_total": Decimal("100.00")}, {}, "budget.max_total"),
        ({}, {"sha": None}, "criteria.required"),
        ({"currency": "EUR"}, {}, "currency.match"),
    ],
)
def test_soft_overrun_steps_up_to_a_human_and_never_pays_silently(intent_kw, contract_kw, rule) -> None:
    rig = Rig(**intent_kw)
    decision = rig.trustee().on_contract(rig.contract(**contract_kw))
    assert decision.action == "escrowed"  # dry-run auto-approves the step-up; the point is who authorized it
    assert rig.rules()[rule] == "fail"
    assert rig.paypal.vault_ids == [None]  # type: ignore[attr-defined]  # no vault id: not paid unattended
    outcomes = [e.payload.get("outcome") for e in rig.ledger.tail("deal-1", kinds=("policy",))]
    assert "step_up" in outcomes and "autonomous" not in outcomes
    notes = " ".join(str(e.payload.get("explanation")) for e in rig.ledger.tail("deal-1", kinds=("note",)))
    assert "simulated" in notes


def test_velocity_rule_counts_autonomous_purchases_in_the_window() -> None:
    rig = Rig(max_purchases=1, window_seconds=3600)
    assert rig.trustee("d1").on_contract(rig.contract("d1")).action == "escrowed"
    second = rig.trustee("d2")
    second.on_contract(rig.contract("d2"))
    assert rig.rules("d1")["velocity.window"] == "pass"
    assert rig.rules("d2")["velocity.window"] == "fail"
    assert rig.paypal.vault_ids[1] is None  # type: ignore[attr-defined]


def test_revoked_funding_steps_up() -> None:
    rig = Rig()
    rig.funding.revoke("org", rig.ref)
    decision = rig.trustee().on_contract(rig.contract())
    assert decision.action == "escrowed" and rig.rules()["funding.active"] == "fail"
    assert rig.paypal.vault_ids == [None]  # type: ignore[attr-defined]


def test_another_principals_funding_cannot_be_named() -> None:
    rig = Rig()
    other = rig.funding.register_fake("someone-else")
    rig.book.put(rig.make_intent(funding_ref=other))
    rig.trustee().on_contract(rig.contract())
    assert rig.rules()["funding.active"] == "fail"


def test_no_intent_means_todays_behaviour_and_no_policy_rows() -> None:
    rig = Rig()
    rig.book = IntentBook()
    rig.autonomy = Autonomy(rig.book, rig.funding, rig.principals)
    decision = rig.trustee().on_contract(rig.contract())
    assert decision.action == "escrowed"
    assert rig.ledger.tail("deal-1", kinds=("policy",)) == []
    assert rig.paypal.vault_ids == [None]  # type: ignore[attr-defined]


def test_capture_rechecks_the_intent_and_holds_for_a_human_when_it_was_revoked() -> None:
    rig = Rig()
    t = rig.trustee()
    c = rig.contract()
    assert t.on_contract(c).action == "escrowed"
    d, a = rig.delivery_and_attest(c)
    t.on_delivery(d, c)
    rig.book.revoke("buyer")  # the principal changes their mind after the funds were held
    decision = t.on_attest(a, c)
    assert decision.action == "disputed" and t.deal.state.value == "DISPUTED"
    assert "capture" not in rig.paypal.calls  # type: ignore[attr-defined]
    assert any("intent.not_revoked" in str(e.payload.get("note")) for e in rig.ledger.tail("deal-1", kinds=("ruling",)))


def test_capture_still_happens_when_the_intent_is_intact() -> None:
    rig = Rig()
    t = rig.trustee()
    c = rig.contract()
    t.on_contract(c)
    d, a = rig.delivery_and_attest(c)
    t.on_delivery(d, c)
    assert t.on_attest(a, c).action == "settled"
    assert rig.paypal.calls == ["authorize", "capture"]  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------------ PayPal shapes


class VaultPayPal:
    """A PayPal sandbox behind MockTransport that knows the vault endpoints."""

    def __init__(self, inline_authorization: bool) -> None:
        self.inline = inline_authorization
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.setup_status = "APPROVED"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content and "oauth2" not in path else {}
        self.bodies.append(body)
        if path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith("/v3/vault/setup-tokens"):
            return httpx.Response(
                201,
                json={
                    "id": "SETUP1",
                    "status": "PAYER_ACTION_REQUIRED",
                    "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/agreements/approve?x=1"}],
                },
            )
        if path.endswith("/v3/vault/setup-tokens/SETUP1"):
            return httpx.Response(200, json={"id": "SETUP1", "status": self.setup_status})
        if path.endswith("/v3/vault/payment-tokens"):
            return httpx.Response(201, json={"id": SECRET_VAULT, "customer": {"id": "C1"}})
        if path.endswith("/v2/checkout/orders"):
            order: dict[str, Any] = {"id": "ORDER1", "status": "COMPLETED" if self.inline else "APPROVED"}
            if self.inline:
                order["purchase_units"] = [{"payments": {"authorizations": [{"id": "AUTH1", "status": "CREATED"}]}}]
            return httpx.Response(201, json=order)
        if path.endswith("/authorize"):
            return httpx.Response(
                201, json={"purchase_units": [{"payments": {"authorizations": [{"id": "AUTH2", "status": "CREATED"}]}}]}
            )
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "CAP1"})
        return httpx.Response(404)

    def client(self) -> PayPalClient:
        c = PayPalClient(client_id="i", client_secret="s", dry_run=False)
        c._http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return c

    def orders(self) -> list[dict[str, Any]]:
        return [
            b for r, b in zip(self.requests, self.bodies, strict=True) if r.url.path.endswith("/v2/checkout/orders")
        ]


@pytest.mark.parametrize(("inline", "authorize_calls", "authz"), [(True, 0, "AUTH1"), (False, 1, "AUTH2")])
def test_vault_order_sends_vault_id_no_experience_context_and_no_approve_link(inline, authorize_calls, authz) -> None:
    fake = VaultPayPal(inline)
    order = fake.client().create_and_authorize(
        amount=Decimal("5.00"), currency="USD", buyer_email="b@x", seller_email="s@x", memo="m",
        request_id="rid", vault_id=SECRET_VAULT,
    )  # fmt: skip
    body = fake.orders()[0]
    assert body["payment_source"] == {"paypal": {"vault_id": SECRET_VAULT}}
    assert "experience_context" not in json.dumps(body)
    assert body["intent"] == "AUTHORIZE"
    assert order.status == "authorized" and order.authorization_id == authz and order.approve_url is None
    assert sum(r.url.path.endswith("/authorize") for r in fake.requests) == authorize_calls
    assert SECRET_VAULT not in json.dumps(order.details)


def test_without_a_vault_id_the_order_still_asks_for_approval() -> None:
    fake = VaultPayPal(False)
    c = fake.client()
    order = c.create_and_authorize(amount=Decimal("5"), currency="USD", buyer_email="b", seller_email="s", memo="m")
    assert "experience_context" in json.dumps(fake.orders()[0]) and order.status in ("created", "authorized")


# -------------------------------------------------------------------------- the service, over HTTP


class World:
    """A platform service with its own signing key, optionally against the vault-aware PayPal mock."""

    def __init__(self, paypal: PayPalClient | None = None, db: str = ":memory:") -> None:
        self.paypal = paypal or SpyPayPal()
        self.store = Store(db)
        self.service = DealService(self.store, self.paypal, Ledger())
        self.app = create_app(ledger=Ledger(), paypal=self.paypal, service=self.service)
        self.http = TestClient(self.app)
        self.rig = Rig()  # only for its keys and helpers
        from trustee.client import TrusteeClient

        self.client = TrusteeClient("http://testserver", http=self.http)
        k = self.rig.keys
        self.client.register_principal("org", k["org"])
        self.client.register_principal("org-s", k["stranger"])
        self.client.register_agent("buyer", k["buyer"], "org", k["org"], ["buyer"])
        self.client.register_agent("seller", k["seller"], "org-s", k["stranger"], ["seller"])
        self.client.publish_mandate(Mandate("buyer", "org", Decimal("500"), grants="spend", version=1), k["org"])
        self.client.publish_mandate(
            Mandate("seller", "org-s", Decimal("500"), grants="receive", version=1), k["stranger"]
        )

    def funding(self) -> str:
        reply = self.client.start_funding("org", self.rig.keys["org"])
        if reply.get("approve_url"):
            self.client._request("GET", f"/v1/funding/return?approval_token_id={self.paypal_setup_id()}")
            reply = self.client._request("GET", f"/v1/funding/{reply['funding_ref']}")
        assert reply["status"] == "active"
        return str(reply["funding_ref"])

    def paypal_setup_id(self) -> str:
        return "SETUP1"

    def intent(self, ref: str, **kw: Any) -> IntentMandate:
        fields: dict[str, Any] = {
            "agent_id": "buyer",
            "principal": "org",
            "instruction": INSTRUCTION,
            "funding_ref": ref,
            "max_price": Decimal("175.00"),
            "allowed_sellers": ["seller"],
            "required_criteria": ["sha256"],
            "issued_at": datetime.now(),
            "expires_at": datetime.now() + timedelta(days=7),
        }
        fields.update(kw)
        return IntentMandate(**fields)

    def contract(
        self,
        deal_id: str,
        price: str = "156.00",
    ) -> Contract:
        now = datetime.now()
        k = self.rig.keys
        return (
            Contract(
                deal_id=deal_id,
                buyer="buyer",
                seller="seller",
                good=good(),
                price=Decimal(price),
                deadline=now + timedelta(hours=1),
                formed_at=now,
            )  # fmt: skip
            .sign("buyer", k["buyer"])
            .sign("seller", k["seller"])
        )

    def settle(self, deal_id: str, price: str = "156.00") -> dict[str, Any]:
        c = self.contract(deal_id, price)
        k = self.rig.keys
        view = self.client.open_deal(c)
        assert view["state"] == "ESCROWED", view
        delivery = Delivery(sender="seller", deal_id=deal_id, deliverable_ref="returns.csv", content_hash=GOOD_HASH)
        self.client.deliver(delivery.sign(k["seller"]))
        attest = Attest(sender="buyer", deal_id=deal_id, verdict="accept", reason="ok")
        return self.client.attest(attest.sign(k["buyer"]))


def test_publish_intent_over_http_then_a_deal_is_paid_unattended_and_receipted() -> None:
    w = World()
    ref = w.funding()
    published = w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    assert len(published["intent_mandate_digest"]) == 64
    final = w.settle("deal-auto")
    assert final["state"] == "SETTLED"
    assert final["authority"]["human_present"] is False and final["authority"]["outcome"] == "autonomous"
    assert w.paypal.vault_ids[0] is not None  # type: ignore[attr-defined]
    receipt = w.client.receipt("deal-auto")
    assert receipt["human_present"] is False and receipt["outcome"] == "settled"
    assert receipt["intent_mandate_digest"] == published["intent_mandate_digest"]
    assert receipt["delivery_sha256"] == GOOD_HASH
    assert {p["rule"] for p in receipt["policy"]} == ALL_RULES
    assert receipt["ledger_head"] and receipt["ledger_events"] > 5


def test_publish_intent_checks_signature_owner_funding_and_replay() -> None:
    w = World()
    ref = w.funding()
    k = w.rig.keys
    intent = w.intent(ref)
    post = lambda body: w.http.post("/v1/intents", json=body)  # noqa: E731
    assert post(intent.public()).status_code == 401  # unsigned
    assert post(intent.sign(k["stranger"]).public()).status_code == 401  # signed by someone else
    assert post(w.intent("fund_nope").sign(k["org"]).public()).status_code == 422  # no such funding source
    assert post(w.intent(ref, agent_id="ghost").sign(k["org"]).public()).status_code == 404
    assert post(w.intent(ref, principal="org-s").sign(k["stranger"]).public()).status_code == 403  # not their agent
    ok = intent.sign(k["org"])
    assert post(ok.public()).status_code == 201
    assert post(ok.public()).status_code == 200  # same document: idempotent
    older = w.intent(ref, issued_at=intent.issued_at - timedelta(days=1)).sign(k["org"])
    assert post(older.public()).status_code == 409  # a replay of an older signed grant
    bad = ok.public()
    bad["max_price"] = "9999.00"
    assert post(bad).status_code == 401  # tampered after signing


def test_revoking_the_intent_over_http_refuses_new_deals_and_holds_open_ones() -> None:
    w = World()
    ref = w.funding()
    w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    c = w.contract("deal-held")
    assert w.client.open_deal(c)["state"] == "ESCROWED"
    w.client.deliver(
        Delivery(sender="seller", deal_id="deal-held", deliverable_ref="returns.csv", content_hash=GOOD_HASH).sign(
            w.rig.keys["seller"]
        )
    )
    assert w.client.revoke_intent("buyer", w.rig.keys["org"])["revoked"] is True
    held = w.client.attest(
        Attest(sender="buyer", deal_id="deal-held", verdict="accept", reason="ok").sign(w.rig.keys["buyer"])
    )
    assert held["state"] == "DISPUTED"  # funds stay held for a human; nothing was captured
    refused = w.http.post("/v1/deals", json=w.contract("deal-new").model_dump(mode="json"))
    assert refused.status_code == 403 and "intent.not_revoked" in json.dumps(refused.json())
    again = w.http.post("/v1/intents", json=w.intent(ref).sign(w.rig.keys["org"]).public())
    assert again.status_code in (200, 201, 409)


def test_overreach_over_http_steps_up_to_the_human() -> None:
    w = World()
    ref = w.funding()
    w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    final = w.settle("deal-over", price="190.00")  # dry-run: the human approval is simulated
    assert final["authority"]["human_present"] is True and final["authority"]["outcome"] == "step_up"
    assert w.client.receipt("deal-over")["human_present"] is True


def test_revoked_funding_steps_up_over_http() -> None:
    w = World()
    ref = w.funding()
    w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    assert w.client.revoke_funding("org", ref, w.rig.keys["org"])["revoked"] is True
    final = w.settle("deal-nofund")
    assert final["authority"]["human_present"] is True
    assert w.http.get(f"/v1/funding/{ref}").json()["status"] == "revoked"
    assert (
        w.http.post(
            f"/v1/principals/org/funding/{ref}/revoke", json={"signature": w.rig.keys["stranger"].sign(b"x")}
        ).status_code
        == 401
    )


def test_deals_without_an_intent_behave_as_before() -> None:
    w = World()
    final = w.settle("deal-plain")
    assert final["authority"] == {
        "human_present": True,
        "intent_mandate_digest": None,
        "outcome": "human_approval",
        "policy": [],
    }
    assert w.paypal.vault_ids == [None]  # type: ignore[attr-defined]
    assert w.client.receipt("deal-plain")["human_present"] is True


# --------------------------------------------------------------------------------- one-time consent flow


def test_funding_consent_flow_vaults_the_token_and_never_returns_it() -> None:
    fake = VaultPayPal(inline_authorization=True)
    w = World(paypal=fake.client())
    k = w.rig.keys
    started = w.client.start_funding("org", k["org"])
    assert started["status"] == "consent_required" and started["approve_url"].startswith("https://sandbox.paypal.com/")
    assert w.http.get(f"/v1/funding/{started['funding_ref']}").json()["status"] == "consent_required"
    # PayPal sends the payer back with the setup token id.
    back = w.http.get(
        "/v1/funding/return", params={"approval_token_id": "SETUP1"}, headers={"Accept": "application/json"}
    )
    assert back.status_code == 200 and back.json()["status"] == "active"
    again = w.http.get(
        "/v1/funding/return", params={"approval_token_id": "SETUP1"}, headers={"Accept": "application/json"}
    )
    assert again.status_code == 200
    assert sum(r.url.path.endswith("/payment-tokens") for r in fake.requests) == 1  # idempotent
    setup_bodies = [b for r, b in zip(fake.requests, fake.bodies, strict=True) if r.url.path.endswith("/setup-tokens")]
    ps = setup_bodies[0]["payment_source"]["paypal"]
    assert ps["usage_type"] == "MERCHANT" and ps["permit_multiple_payment_tokens"] is True
    assert ps["experience_context"]["shipping_preference"] == "NO_SHIPPING"
    assert w.store.get_vault_id(started["funding_ref"], "org") == SECRET_VAULT  # stored, server side only
    for body in (started, back.json(), again.json(), w.http.get(f"/v1/funding/{started['funding_ref']}").json()):
        assert SECRET_VAULT not in json.dumps(body)


def test_consent_not_yet_approved_is_a_409_and_stores_nothing() -> None:
    fake = VaultPayPal(True)
    fake.setup_status = "PAYER_ACTION_REQUIRED"
    w = World(paypal=fake.client())
    started = w.client.start_funding("org", w.rig.keys["org"])
    r = w.http.get("/v1/funding/return", params={"approval_token_id": "SETUP1"}, headers={"Accept": "application/json"})
    assert r.status_code == 409
    assert w.store.get_vault_id(started["funding_ref"], "org") is None
    assert w.http.get("/v1/funding/return", params={"approval_token_id": "nope"}).status_code == 404


def test_vaulted_deal_end_to_end_against_the_mock_never_leaks_the_vault_id() -> None:
    fake = VaultPayPal(inline_authorization=False)
    w = World(paypal=fake.client())
    ref = w.funding()
    w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    final = w.settle("deal-vault")
    assert final["state"] == "SETTLED"
    order = fake.orders()[0]
    assert order["payment_source"] == {"paypal": {"vault_id": SECRET_VAULT}}
    assert "experience_context" not in json.dumps(order)
    # Every place a secret could surface: API views, A2A task, receipt, events, ledger, stored snapshot.
    surfaces: list[str] = [
        json.dumps(final),
        json.dumps(w.client.get_deal("deal-vault")),
        json.dumps(w.client.receipt("deal-vault")),
        json.dumps(w.client.events("deal-vault", limit=1000)),
        json.dumps([e.__dict__ for e in w.service.ledger.tail()], default=str),
        json.dumps(w.store.get_deal("deal-vault"), default=str),
        w.http.get("/.well-known/agent.json").text,
        json.dumps(
            w.http.post(
                "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "deal-vault"}}
            ).json()
        ),
        json.dumps(w.client.agent_card()),
    ]
    for text in surfaces:
        assert SECRET_VAULT not in text


# ------------------------------------------------------------------------------- hash chain and receipts


def test_ledger_chain_verifies_and_detects_tampering() -> None:
    ledger = Ledger()
    for i in range(5):
        ledger.append("d1", "trustee", "note", step=i, price=Decimal("1.50"))
    ledger.append("d2", "trustee", "note", step=0)
    assert ledger.verify_chain("d1") == (True, "ok") and ledger.verify_chain("d2")[0]
    events = [e.__dict__.copy() for e in ledger.tail("d1")]
    assert verify_chain(events)[0]
    edited = [dict(e) for e in events]
    edited[2]["payload"] = {**edited[2]["payload"], "step": 99}
    assert not verify_chain(edited)[0]
    assert not verify_chain(events[:2] + events[3:])[0]  # a dropped event
    assert not verify_chain(events[1:])[0]  # a truncated head
    swapped = [events[0], events[2], events[1], *events[3:]]
    assert not verify_chain(swapped)[0]
    forged = [dict(e) for e in events]
    forged[1]["hash"] = "f" * 64
    assert not verify_chain(forged)[0]


def test_persisted_events_keep_the_chain_and_verify_after_a_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    ledger = Ledger()
    store.attach(ledger)
    ledger.append("d1", "trustee", "paypal", op="authorize", amount=Decimal("156.00"), when=datetime(2026, 1, 1))
    ledger.append("d1", "trustee", "state", state_to="ESCROWED")
    rows = store.events("d1")
    assert all(r["hash"] and r["prev_hash"] for r in rows)
    assert verify_chain(rows)[0]
    store.close()
    reopened = Store(tmp_path / "t.db")  # additive migration is a no-op the second time
    assert verify_chain(reopened.events("d1"))[0]


def test_old_databases_gain_the_hash_columns() -> None:
    import sqlite3

    path = ":memory:"
    store = Store(path)
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(events)")}  # noqa: SLF001
    assert {"prev_hash", "hash"} <= cols
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE events (seq INTEGER PRIMARY KEY, ts TEXT, deal_id TEXT, actor TEXT, kind TEXT, payload TEXT)"
    )
    assert "hash" not in {r[1] for r in conn.execute("PRAGMA table_info(events)")}


def settled_receipt(w: World, deal_id: str = "deal-r") -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    ref = w.funding()
    w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    w.settle(deal_id)
    return w.client.receipt(deal_id), w.client.get_deal(deal_id), w.client.events(deal_id, limit=1000)["events"]


def test_receipt_verifies_offline_and_every_field_is_bound_by_the_signature(tmp_path: Path) -> None:
    w = World()
    receipt, _view, events = settled_receipt(w)
    key = w.http.get("/.well-known/trustee-key.json").json()["public_key"]
    assert (
        key == receipt["trustee_key"] == w.http.get("/.well-known/agent.json").json()["receiptSigningKey"]["publicKey"]
    )
    report = verify_cli.verify_receipt(receipt, public_key=key, events=events)
    assert report.ok, report.as_dict()
    signed_fields = (
        "outcome amount buyer seller contract_digest intent_mandate_digest human_present delivery_sha256 policy "
        "paypal ledger_head ledger_events issued_at deal_id currency"
    ).split()
    for field in signed_fields:
        mutated = {**receipt, field: True if field == "human_present" else "tampered"}
        assert not verify_cli.verify_receipt(mutated, public_key=key).ok, field
    other = AgentKey.generate().public_key_b64
    assert not verify_cli.verify_receipt(receipt, public_key=other).ok  # not the pinned Trustee key
    # The same through the command line, offline, from files.
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    (tmp_path / "events.json").write_text(json.dumps({"deal_id": "deal-r", "events": events}))
    # `--key=` form: a base64url key can start with "-", which argparse would read as an option after a bare `--key`.
    argv = [str(tmp_path / "receipt.json"), f"--key={key}", "--events", str(tmp_path / "events.json")]
    assert verify_cli.main(argv) == 0
    (tmp_path / "receipt.json").write_text(json.dumps({**receipt, "amount": "1.00"}))
    assert verify_cli.main(argv) == 1


def test_receipt_checks_contract_and_intent_documents_and_the_event_chain() -> None:
    w = World()
    receipt, _view, events = settled_receipt(w)
    key = receipt["trustee_key"]
    contract_doc = w.service.record("deal-r")["contract"]
    assert verify_cli.verify_receipt(receipt, public_key=key, contract=contract_doc).ok
    wrong = {**contract_doc, "price": "1.00"}
    assert not verify_cli.verify_receipt(receipt, public_key=key, contract=wrong).ok
    intent_doc = w.service.intents.get("buyer").public()  # type: ignore[union-attr]
    assert verify_cli.verify_receipt(
        receipt, public_key=key, intent=intent_doc, principal_key=w.rig.keys["org"].public_key_b64
    ).ok
    assert not verify_cli.verify_receipt(receipt, public_key=key, intent={**intent_doc, "max_price": "999.00"}).ok
    broken = [dict(e) for e in events]
    broken[3]["payload"] = {**broken[3]["payload"], "note": "edited"}
    assert not verify_cli.verify_receipt(receipt, public_key=key, events=broken).ok
    assert not verify_cli.verify_receipt(receipt, public_key=key, events=events[:-3]).ok
    # server-side convenience endpoint agrees
    ok = w.http.post("/v1/receipts/verify", json={"receipt": receipt, "events": events}).json()
    assert ok["ok"] is True
    bad = w.http.post("/v1/receipts/verify", json={"receipt": {**receipt, "amount": "1.00"}}).json()
    assert bad["ok"] is False


def test_voided_deal_gets_a_receipt_too() -> None:
    w = World()
    ref = w.funding()
    w.client.publish_intent(w.intent(ref), w.rig.keys["org"])
    k = w.rig.keys
    w.client.open_deal(w.contract("deal-v"))
    w.client.deliver(
        Delivery(sender="seller", deal_id="deal-v", deliverable_ref="returns.csv", content_hash="b" * 64).sign(
            k["seller"]
        )
    )
    w.client.attest(
        Attest(
            sender="buyer",
            deal_id="deal-v",
            verdict="reject",
            reason="hash mismatch",
            evidence={"sha256_check": "mismatch"},
        ).sign(k["buyer"])  # fmt: skip
    )
    receipt = w.client.receipt("deal-v")
    assert receipt["outcome"] == "voided" and receipt["paypal"]["voided"] is True
    assert verify_cli.verify_receipt(receipt, public_key=receipt["trustee_key"]).ok
    assert w.http.get("/v1/deals/deal-unknown/receipt").status_code == 404


def test_receipt_is_a_409_until_the_deal_ends() -> None:
    w = World()
    w.client.open_deal(w.contract("deal-open"))
    assert w.http.get("/v1/deals/deal-open/receipt").status_code == 409


def test_a2a_settlement_artifact_carries_the_signed_receipt() -> None:
    w = World()
    receipt, _view, _events = settled_receipt(w, "deal-a2a")
    task = w.http.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "deal-a2a"}}
    ).json()
    result = task["result"]
    art = next(a for a in result["artifacts"] if a["name"] == "settlement-receipt")
    data = art["parts"][0]["data"]
    assert data["signedReceipt"] == receipt and data["humanPresent"] is False
    assert data["intentMandateDigest"] == receipt["intent_mandate_digest"]


def test_trustee_signing_key_comes_from_env_then_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import base64

    fixed = AgentKey.generate()
    monkeypatch.setenv("TRUSTEE_SIGNING_KEY", base64.b64encode(fixed.private_bytes()).decode())
    assert load_signing_key(Store(":memory:")).public_key_b64 == fixed.public_key_b64
    monkeypatch.delenv("TRUSTEE_SIGNING_KEY")
    store = Store(tmp_path / "k.db")
    first = load_signing_key(store)
    assert load_signing_key(store).public_key_b64 == first.public_key_b64  # kept across loads
    with pytest.raises(ValueError):
        monkeypatch.setenv("TRUSTEE_SIGNING_KEY", "not-a-key")
        load_signing_key(None)


def test_restart_rehydrates_intents_and_the_receipt_survives(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")
    w = World(db=db)
    receipt, _v, _e = settled_receipt(w, "deal-keep")
    w2 = DealService(Store(db), SpyPayPal(), Ledger())
    assert w2.intents.get("buyer") is not None
    assert w2.receipt("deal-keep") == receipt
    assert w2.signing_key.public_key_b64 == receipt["trustee_key"]


def test_build_receipt_refuses_a_deal_that_is_not_finished() -> None:
    rig = Rig()
    t = rig.trustee()
    c = rig.contract()
    t.on_contract(c)
    with pytest.raises(ValueError):
        build_receipt(t.snapshot(), c, key=AgentKey.generate(), ledger_head="x", ledger_events=1)


# ---------------------------------------------------------------------------------------- the arena


def arena_runner() -> DealRunner:
    return DealRunner(ledger=Ledger(), paypal=PayPalClient(dry_run=True))


def tape(runner: DealRunner, deal_id: str) -> list[dict[str, Any]]:
    return [to_row(e, i) for i, e in enumerate(runner.ledger.tail(deal_id))]


@pytest.mark.parametrize(("name", "state"), [("honest", "SETTLED"), ("cheat", "VOIDED")])
def test_arena_runs_pay_under_the_intent_with_no_human_click(name: str, state: str) -> None:
    runner = arena_runner()
    result = runner.run(get_scenario(name), mode="scripted")
    assert result.state == state and result.error is None
    rows = tape(runner, result.deal_id)
    rules = {r["rule"]: r["result"] for r in rows if r["rule"]}
    assert set(rules) == ALL_RULES and set(rules.values()) == {"pass"}
    consent = [r for r in rows if r["event"] == "intent_consent"]
    assert consent and "one-time consent signed by the human" in consent[0]["note"]
    states = [r["note"] for r in rows if r["kind"] == "state" and r["state_to"] == "ESCROWED"]
    assert states == ["funds held under the intent mandate, no human click"]
    assert result.human_present is False and result.receipt_verified is True
    assert result.receipt is not None and result.receipt["outcome"] == ("settled" if state == "SETTLED" else "voided")
    assert [r["authority"] for r in rows if r["kind"] == "receipt"] == ["intent_mandate"]
    assert SECRET_VAULT not in json.dumps(result.snapshot(), default=str)


def test_arena_receipt_verifies_with_the_cli_from_the_tape(tmp_path: Path) -> None:
    runner = arena_runner()
    result = runner.run(get_scenario("honest"), mode="scripted")
    events = [e.__dict__ for e in runner.ledger.tail(result.deal_id)]
    (tmp_path / "r.json").write_text(json.dumps(result.receipt))
    (tmp_path / "e.json").write_text(json.dumps(events, default=str))
    key = runner.signing_key.public_key_b64
    assert verify_cli.main([str(tmp_path / "r.json"), f"--key={key}", "--events", str(tmp_path / "e.json")]) == 0


def test_arena_overreach_steps_up_and_says_so_on_the_tape() -> None:
    runner = arena_runner()
    result = runner.run(get_scenario("overreach"), mode="scripted")
    assert result.state == "SETTLED" and result.human_present is True
    rows = tape(runner, result.deal_id)
    failed = [r["rule"] for r in rows if r["result"] == "fail"]
    assert failed == ["price.max_price"]
    assert any("a human must approve" in r["note"] for r in rows)
    assert any("simulated" in r["note"] for r in rows)
    assert [r["authority"] for r in rows if r["kind"] == "receipt"] == ["human"]
    assert result.price == "190.00"


def test_arena_forged_is_still_refused_before_paypal_and_without_policy_rows() -> None:
    paypal = SpyPayPal()
    runner = DealRunner(ledger=Ledger(), paypal=paypal)
    result = runner.run(get_scenario("forged"), mode="scripted")
    assert result.outcome == "rejected_identity" and paypal.calls == []
    assert not [r for r in tape(runner, result.deal_id) if r["rule"]]


def test_arena_a2a_and_outside_settle_autonomously_over_http() -> None:
    from test_a2a_network import arena

    app, _http = arena()
    runner = app.state.runner
    for name in ("a2a", "outside"):
        result = runner.run(get_scenario(name), mode="scripted")
        assert result.state == "SETTLED" and result.error is None and result.human_present is False, name
        assert result.receipt_verified is True
        rows = tape(runner, result.deal_id)
        assert {r["rule"] for r in rows if r["rule"]} == ALL_RULES
        assert not [r for r in rows if r["kind"] == "api" and "paypal/return" in r["event"]]  # nobody "clicked"
        assert any(r["event"] == "intent_consent" for r in rows)
    published = [r for r in tape(runner, result.deal_id)]
    assert published


def test_arena_receipt_endpoint_and_run_snapshot() -> None:
    from test_a2a_network import arena

    app, http = arena()
    run_id = http.post("/api/runs", json={"scenario": "honest", "mode": "scripted"}).json()["run_id"]
    for _ in range(100):
        snap = http.get(f"/api/runs/{run_id}").json()
        if snap["state"] in ("SETTLED", "VOIDED", "FAILED"):
            break
        time.sleep(0.05)
    assert snap["human_present"] is False and snap["receipt_verified"] is True
    assert http.get(f"/api/runs/{run_id}/receipt").json() == snap["receipt"]
    key = http.get("/.well-known/trustee-key.json").json()["public_key"]
    assert verify_cli.verify_receipt(snap["receipt"], public_key=key).ok  # the arena signs with the platform's key
    scenarios = {s["name"]: s for s in http.get("/api/scenarios").json()["scenarios"]}
    assert scenarios["overreach"]["instruction"] and "175" in scenarios["honest"]["instruction"]


def test_demo_vault_is_registered_for_the_principal_and_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYPAL_DEMO_VAULT_ID", SECRET_VAULT)
    store = Store(":memory:")
    reg = FundingRegistry(store, PayPalClient(dry_run=True))
    ref = reg.register_demo("org")
    assert ref and reg.is_active("org", ref) and not reg.is_active("other", ref)
    assert reg.vault_id("org", ref) == SECRET_VAULT
    assert SECRET_VAULT not in json.dumps(reg.status(ref)) and SECRET_VAULT not in json.dumps(store.funding_public(ref))
    monkeypatch.delenv("PAYPAL_DEMO_VAULT_ID")
    assert FundingRegistry(store, PayPalClient(dry_run=True)).register_demo("org") is None


def test_real_paypal_without_a_demo_vault_keeps_the_approval_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYPAL_DEMO_VAULT_ID", raising=False)
    fake = VaultPayPal(True)
    runner = DealRunner(ledger=Ledger(), paypal=fake.client())
    # A sandbox client with no demo vault: no funding, so the run asks a human (it would wait on approval).
    from trustee.arena.approval import ApprovalWait

    runner.approval_wait = ApprovalWait(timeout_seconds=0.05, poll_seconds=0.01)
    result = runner.run(get_scenario("honest"), mode="scripted")
    rows = tape(runner, result.deal_id)
    assert any(r["event"] == "no_funding" for r in rows)
    assert not [r for r in rows if r["rule"]]
    assert all("vault_id" not in json.dumps(b) for b in fake.bodies)


def test_every_hard_rule_is_marked_hard_and_every_soft_rule_soft() -> None:
    rig = Rig()
    decision = evaluate_intent(
        rig.intent, rig.contract(), registry=rig.registry, principals=rig.principals, revoked=False,
        committed=Decimal(0), recent_purchases=0, funding_active=True, now=T0,
    )  # fmt: skip
    assert decision.outcome == "autonomous" and {r.rule for r in decision.rules} == ALL_RULES
    hard = {r.rule for r in decision.rules if r.hard}
    assert hard == {
        "signature.principal_intent", "signature.principal_agent", "signature.agent_contract",
        "intent.not_revoked", "intent.not_expired", "intent.require_escrow", "seller.allowed",
    }  # fmt: skip
