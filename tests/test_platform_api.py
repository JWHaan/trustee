"""Public deal API: signed identities, mandates, escrow lifecycle, A2A card, restart durability."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trustee.arena.api import create_app
from trustee.identity import AgentKey
from trustee.ledger import Ledger
from trustee.mandate import Mandate
from trustee.paypal import EscrowOrder, PayPalClient, PayPalError
from trustee.platform import DealService
from trustee.platform.wire import (
    agent_signing_bytes,
    mandate_to_dict,
    principal_signing_bytes,
    revoke_signing_bytes,
)
from trustee.protocol import Attest, Contract, Delivery, GoodSpec
from trustee.store import Store

TOKEN = "s3cret-operator"
GOOD_HASH = "a" * 64
T0 = datetime.now()  # fixed, so equal calls to World.contract() yield equal digests


class CountingPayPal(PayPalClient):
    """Dry-run PayPal that counts every money call."""

    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.calls = {"authorize": 0, "capture": 0, "void": 0}
        self.fail_authorize = 0

    def create_and_authorize(self, *args, **kwargs) -> EscrowOrder:
        self.calls["authorize"] += 1
        if self.fail_authorize:
            self.fail_authorize -= 1
            raise PayPalError("boom", 500, "")
        return super().create_and_authorize(*args, **kwargs)

    def capture(self, order, **kwargs) -> EscrowOrder:
        self.calls["capture"] += 1
        return super().capture(order, **kwargs)

    def void(self, order, reason: str = "", **kwargs) -> EscrowOrder:
        self.calls["void"] += 1
        return super().void(order, reason, **kwargs)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


class World:
    """One service, one TestClient, and two registered parties with mandates."""

    def __init__(self, db: str | Path = ":memory:", paypal: CountingPayPal | None = None, token: str | None = TOKEN):
        self.paypal = paypal or CountingPayPal()
        self.store = Store(db)
        self.service = DealService(self.store, self.paypal, Ledger(), operator_token=token)
        self.app = create_app(ledger=Ledger(), paypal=self.paypal, service=self.service)
        self.http = TestClient(self.app)
        self.keys = {name: AgentKey.generate() for name in ("org-b", "org-s", "buyer", "seller")}

    # -- registration -------------------------------------------------------
    def post(self, path: str, body: dict, **kw):
        return self.http.post(path, json=body, **kw)

    def principal(self, name: str, key: AgentKey, **kw):
        pub = key.public_key_b64
        return self.post(
            "/v1/principals",
            {"principal": name, "public_key": pub, "proof": key.sign(principal_signing_bytes(name, pub))},
            **kw,
        )

    def agent(self, agent_id: str, key: AgentKey, principal: str, principal_key: AgentKey, roles: list[str]):
        pub = key.public_key_b64
        body = agent_signing_bytes(agent_id, pub, principal, roles, None)
        return self.post(
            "/v1/agents",
            {
                "agent_id": agent_id,
                "public_key": pub,
                "principal": principal,
                "roles": roles,
                "paypal_email": None,
                "proof": key.sign(body),
                "principal_signature": principal_key.sign(body),
            },
        )

    def mandate(self, m: Mandate, key: AgentKey):
        return self.post("/v1/mandates", mandate_to_dict(m.sign(key)))

    def setup(self, buyer_cap: str = "500.00", max_total: str | None = None) -> World:
        k = self.keys
        auth = {"headers": {"Authorization": f"Bearer {TOKEN}"}}
        assert self.principal("org-b", k["org-b"], **auth).status_code == 201
        assert self.principal("org-s", k["org-s"], **auth).status_code == 201
        assert self.agent("buyer", k["buyer"], "org-b", k["org-b"], ["buyer"]).status_code == 201
        assert self.agent("seller", k["seller"], "org-s", k["org-s"], ["seller"]).status_code == 201
        cats = frozenset({"code"})
        buyer_mandate = Mandate(
            agent_id="buyer",
            principal="org-b",
            max_amount=Decimal(buyer_cap),
            allowed_categories=cats,
            allowed_counterparties=frozenset({"seller"}),
            grants="spend",
            max_total=Decimal(max_total) if max_total else None,
        )
        seller_mandate = Mandate(
            agent_id="seller",
            principal="org-s",
            max_amount=Decimal("500.00"),
            allowed_categories=cats,
            allowed_counterparties=frozenset({"buyer"}),
            grants="receive",
        )
        assert self.mandate(buyer_mandate, k["org-b"]).status_code == 201
        assert self.mandate(seller_mandate, k["org-s"]).status_code == 201
        return self

    # -- deals --------------------------------------------------------------
    def contract(self, deal_id: str = "deal-1", price: str = "25.00", hours: int = 1, sign: bool = True) -> Contract:
        now = T0
        c = Contract(
            deal_id=deal_id,
            buyer="buyer",
            seller="seller",
            good=GoodSpec(
                good_id="g1",
                description="module",
                category="code",
                acceptance_criteria=["sha256 matches"],
                deliverable_ref="m.zip",
                sha256=GOOD_HASH,
            ),
            price=Decimal(price),
            deadline=now + timedelta(hours=hours),
            formed_at=now,
        )
        return c.sign("buyer", self.keys["buyer"]).sign("seller", self.keys["seller"]) if sign else c

    def open(self, c: Contract):
        return self.post("/v1/deals", c.model_dump(mode="json"))

    def deliver(self, deal_id: str, signer: str = "seller", content_hash: str = GOOD_HASH):
        d = Delivery(sender=signer, deal_id=deal_id, deliverable_ref="m.zip", content_hash=content_hash)
        d = d.sign(self.keys[signer])
        return self.post(f"/v1/deals/{deal_id}/delivery", d.model_dump(mode="json"))

    def attest(self, deal_id: str, verdict: str = "accept", evidence: dict | None = None, signer: str = "buyer"):
        a = Attest(sender=signer, deal_id=deal_id, verdict=verdict, reason="because", evidence=evidence or {})
        a = a.sign(self.keys[signer])
        return self.post(f"/v1/deals/{deal_id}/attest", a.model_dump(mode="json"))


@pytest.fixture
def world() -> World:
    return World().setup()


# ----------------------------------------------------------------- lifecycles


def test_honest_deal_settles_over_http(world: World):
    r = world.open(world.contract())
    assert r.status_code == 201
    assert r.json()["state"] == "ESCROWED"
    assert r.json()["escrow"]["status"] == "authorized"
    assert world.deliver("deal-1").json()["state"] == "DELIVERED"
    final = world.attest("deal-1", evidence={"sha256_check": "match"})
    assert final.status_code == 200
    body = final.json()
    assert body["state"] == "SETTLED"
    assert body["escrow"]["status"] == "captured"
    assert body["contract"]["digest"] == world.contract().digest()
    assert world.paypal.calls == {"authorize": 1, "capture": 1, "void": 0}
    assert world.http.get("/v1/deals/deal-1").json()["state"] == "SETTLED"
    kinds = [e["kind"] for e in world.http.get("/v1/deals/deal-1/events").json()["events"]]
    assert "paypal" in kinds and "state" in kinds


def test_cheat_delivery_is_voided(world: World):
    world.open(world.contract())
    world.deliver("deal-1", content_hash="b" * 64)
    r = world.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "VOIDED"
    assert body["escrow"]["status"] == "voided"
    assert body["ruling"]["decision"] == "release"
    assert world.paypal.calls == {"authorize": 1, "capture": 0, "void": 1}


def test_attest_before_delivery_is_refused_without_moving_money(world: World):
    world.open(world.contract())
    r = world.attest("deal-1")
    assert r.status_code == 409
    assert world.paypal.calls["capture"] == 0
    assert world.http.get("/v1/deals/deal-1").json()["state"] == "ESCROWED"


# ---------------------------------------------------------------- the gates


@pytest.mark.parametrize("tamper", ["unsigned", "forged"])
def test_unsigned_or_forged_contract_is_401_and_never_touches_paypal(world: World, tamper: str):
    c = world.contract(sign=False)
    if tamper == "forged":
        c = c.sign("buyer", AgentKey.generate()).sign("seller", world.keys["seller"])
    r = world.open(c)
    assert r.status_code == 401
    assert world.paypal.total == 0
    assert world.http.get("/v1/deals/deal-1").status_code == 404
    # The refused attempt must not squat on the deal id.
    assert world.open(world.contract()).status_code == 201


def test_mandate_over_cap_is_403_with_violations(world: World):
    r = world.open(world.contract(price="900.00"))
    assert r.status_code == 403
    assert any("exceeds cap" in v for v in r.json()["violations"])
    assert world.paypal.total == 0


def test_cumulative_budget_is_enforced_across_deals():
    w = World().setup(max_total="40.00")
    assert w.open(w.contract("d1", "25.00")).status_code == 201
    r = w.open(w.contract("d2", "25.00"))
    assert r.status_code == 403
    assert any("budget exceeded" in v for v in r.json()["violations"])
    assert w.paypal.calls["authorize"] == 1


def test_duplicate_open_is_idempotent_and_different_digest_conflicts(world: World):
    c = world.contract()
    first = world.open(c)
    second = world.open(c)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert world.paypal.calls["authorize"] == 1
    other = world.contract(price="26.00")
    assert world.open(other).status_code == 409
    assert world.paypal.calls["authorize"] == 1


def test_delivery_signed_by_buyer_is_401(world: World):
    world.open(world.contract())
    r = world.deliver("deal-1", signer="buyer")
    assert r.status_code == 401
    assert world.http.get("/v1/deals/deal-1").json()["state"] == "ESCROWED"


def test_attest_signed_by_seller_is_401(world: World):
    world.open(world.contract())
    world.deliver("deal-1")
    assert world.attest("deal-1", signer="seller").status_code == 401
    assert world.paypal.calls["capture"] == 0


def test_body_deal_id_must_match_path(world: World):
    world.open(world.contract())
    d = Delivery(sender="seller", deal_id="deal-1", deliverable_ref="m.zip", content_hash=GOOD_HASH)
    d = d.sign(world.keys["seller"])
    assert world.post("/v1/deals/other/delivery", d.model_dump(mode="json")).status_code == 400


def test_unknown_deal_is_404(world: World):
    assert world.http.get("/v1/deals/nope").status_code == 404
    assert world.http.get("/v1/deals/nope/events").status_code == 404
    assert world.deliver("nope").status_code == 404


def test_paypal_failure_is_502_with_view_and_retry_succeeds():
    pp = CountingPayPal()
    pp.fail_authorize = 1
    w = World(paypal=pp).setup()
    c = w.contract()
    r = w.open(c)
    assert r.status_code == 502
    assert r.json()["deal"]["state"] == "CONTRACTED"
    assert r.json()["deal"]["escrow"] is None
    again = w.open(c)  # same digest: retries the same escrow
    assert again.status_code == 200
    assert again.json()["state"] == "ESCROWED"
    assert pp.calls["authorize"] == 2


# ---------------------------------------------------------------- identities


def test_registration_requires_valid_proofs_and_blocks_key_swap(world: World):
    k = world.keys
    bad = world.post(
        "/v1/principals",
        {"principal": "org-x", "public_key": k["org-b"].public_key_b64, "proof": k["org-s"].sign(b"nope")},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert bad.status_code == 401
    # Operator token is enforced when configured.
    assert world.principal("org-y", AgentKey.generate()).status_code == 401
    # Same principal, different key.
    assert (
        world.principal("org-b", AgentKey.generate(), headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 409
    )
    # Agent key swap.
    assert world.agent("buyer", AgentKey.generate(), "org-b", k["org-b"], ["buyer"]).status_code == 409
    # Principal must vouch.
    assert world.agent("evil", AgentKey.generate(), "org-b", AgentKey.generate(), ["buyer"]).status_code == 401
    pub = world.http.get("/v1/agents/buyer").json()
    assert pub["principal"] == "org-b" and "private" not in str(pub)
    assert world.http.get("/v1/agents/ghost").status_code == 404


def test_open_registration_without_operator_token():
    w = World(token=None)
    assert w.principal("org-open", w.keys["org-b"]).status_code == 201
    assert w.principal("org-open", w.keys["org-b"]).status_code == 200  # same key: idempotent


def test_mandate_must_be_signed_by_the_agents_principal(world: World):
    m = Mandate(agent_id="buyer", principal="org-b", max_amount=Decimal("999"), grants="spend")
    assert world.mandate(m, world.keys["org-s"]).status_code == 401  # wrong signer
    assert world.mandate(m, AgentKey.generate()).status_code == 401
    other = Mandate(agent_id="buyer", principal="org-s", max_amount=Decimal("999"), grants="spend")
    assert world.mandate(other, world.keys["org-s"]).status_code == 403  # agent belongs to org-b
    assert world.open(world.contract(price="900.00")).status_code == 403  # the real cap still applies


def test_revoked_mandate_blocks_new_deals_and_stays_revoked(world: World):
    k = world.keys
    sig = k["org-b"].sign(revoke_signing_bytes("buyer"))
    assert world.post("/v1/mandates/buyer/revoke", {"signature": k["org-s"].sign(b"x")}).status_code == 401
    assert world.post("/v1/mandates/buyer/revoke", {"signature": sig}).status_code == 200
    r = world.open(world.contract())
    assert r.status_code == 403 and any("revoked" in v for v in r.json()["violations"])
    again = Mandate(agent_id="buyer", principal="org-b", max_amount=Decimal("500"), grants="spend")
    assert world.mandate(again, k["org-b"]).status_code == 409  # a replayed old mandate cannot undo it
    assert world.paypal.total == 0


# ---------------------------------------------------------------- operator


def test_resolve_needs_the_operator_token_and_works_on_a_disputed_deal():
    w = World().setup()
    w.open(w.contract())
    w.deliver("deal-1")
    # No objective breach evidence: the built-in policy upholds, so the deal parks in DISPUTED with funds held.
    disputed = w.attest("deal-1", verdict="reject", evidence={})
    assert disputed.json()["state"] == "DISPUTED"
    body = {"decision": "settle", "operator": "ops@x", "reason": "seller is right"}
    assert w.post("/v1/deals/deal-1/resolve", body).status_code == 401
    assert w.post("/v1/deals/deal-1/resolve", body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = w.post("/v1/deals/deal-1/resolve", body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert ok.json()["state"] == "SETTLED"
    assert w.paypal.calls["capture"] == 1
    # Terminal now: a second override is a no-op, not a second capture.
    assert w.post("/v1/deals/deal-1/resolve", body, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert w.paypal.calls["capture"] == 1


def test_resolve_is_forbidden_when_no_operator_token_is_configured():
    w = World(token=None).setup()
    w.open(w.contract())
    w.deliver("deal-1")
    assert w.attest("deal-1", verdict="reject").json()["state"] == "DISPUTED"
    body = {"decision": "release", "operator": "ops", "reason": ""}
    assert w.post("/v1/deals/deal-1/resolve", body).status_code == 403
    assert w.post("/v1/deals/deal-1/resolve", body, headers={"Authorization": "Bearer "}).status_code == 403
    assert w.http.get("/v1/deals/deal-1").json()["state"] == "DISPUTED"
    assert w.paypal.calls["void"] == 0


def test_resolve_on_non_disputed_deal_is_409(world: World):
    world.open(world.contract())
    r = world.post(
        "/v1/deals/deal-1/resolve",
        {"decision": "release", "operator": "ops", "reason": ""},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------- agent card


def test_agent_card_fields_and_skills(world: World, monkeypatch):
    monkeypatch.delenv("TRUSTEE_PUBLIC_URL", raising=False)
    for path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
        card = world.http.get(path).json()
        for field in (
            "name",
            "description",
            "version",
            "capabilities",
            "securitySchemes",
            "defaultInputModes",
            "defaultOutputModes",
            "skills",
            "supportedInterfaces",
            "protocolVersion",
            "url",
            "preferredTransport",
        ):
            assert field in card, field
        # The A2A JSON-RPC binding (POST /a2a) is the preferred interface; /v1 stays listed as the REST one.
        assert card["preferredTransport"] == "JSONRPC"
        assert card["url"] == "http://testserver/a2a"
        assert card["supportedInterfaces"][0]["url"] == card["url"]
        assert {"url": "http://testserver/v1", "protocolBinding": "HTTP+JSON", "protocolVersion": "0.3"} in card[
            "supportedInterfaces"
        ]
        assert card["capabilities"]["streaming"] is False and card["capabilities"]["pushNotifications"] is True
        assert {s["id"] for s in card["skills"]} == {
            "register-principal",
            "register-agent",
            "publish-mandate",
            "revoke-mandate",
            "open-escrow",
            "record-delivery",
            "attest-delivery",
            "get-deal",
        }
        for s in card["skills"]:
            assert {"id", "name", "description", "tags", "examples", "inputModes", "outputModes"} <= s.keys()
        assert "canonical" in card["description"].lower()


def test_agent_card_url_comes_from_env(world: World, monkeypatch):
    monkeypatch.setenv("TRUSTEE_PUBLIC_URL", "https://trustee.example.com/")
    card = world.http.get("/.well-known/agent.json").json()
    assert card["url"] == "https://trustee.example.com/a2a"
    assert card["additionalInterfaces"][1]["url"] == "https://trustee.example.com/v1"


def test_healthz_reports_store_and_existing_routes_still_work(world: World):
    health = world.http.get("/healthz").json()
    assert health["ok"] is True and health["store"] == "in-memory"
    assert world.http.get("/api/scenarios").status_code == 200


# ---------------------------------------------------------------- durability


def test_restart_resumes_an_escrowed_deal_and_captures_exactly_once(tmp_path: Path):
    db = tmp_path / "trustee.db"
    pp = CountingPayPal()  # PayPal outlives the Trustee process
    w1 = World(db, paypal=pp).setup(max_total="30.00")
    assert w1.open(w1.contract()).json()["state"] == "ESCROWED"
    w1.http.close()
    w1.store.close()

    w2 = World(db, paypal=pp)
    w2.keys = w1.keys  # same parties, new process
    assert len(w2.service.sweeper) == 1
    view = w2.http.get("/v1/deals/deal-1").json()
    assert view["state"] == "ESCROWED" and view["escrow"]["status"] == "authorized"
    # The budget reservation came back: a second 25 USD deal would exceed the 30 USD total.
    assert w2.open(w2.contract("deal-2")).status_code == 403
    # The same open request after the restart is still idempotent.
    assert w2.open(w2.contract()).status_code == 200
    assert pp.calls["authorize"] == 1
    assert w2.deliver("deal-1").json()["state"] == "DELIVERED"
    settled = w2.attest("deal-1", evidence={"sha256_check": "match"})
    assert settled.json()["state"] == "SETTLED"
    assert pp.calls == {"authorize": 1, "capture": 1, "void": 0}
    w2.http.close()
    w2.store.close()

    # A third process sees the terminal deal and the spent budget, and never captures again.
    w3 = World(db, paypal=pp)
    w3.keys = w1.keys
    assert w3.http.get("/v1/deals/deal-1").json()["state"] == "SETTLED"
    assert w3.attest("deal-1").status_code == 200  # replay is a no-op
    assert pp.calls["capture"] == 1
    assert w3.open(w3.contract("deal-3")).status_code == 403  # 25 spent + 25 > 30
    assert w3.http.get("/v1/deals/deal-1/events").json()["events"]  # tape survived
    w3.store.close()


def test_sweep_voids_a_deal_past_its_deadline_and_persists_it(tmp_path: Path):
    db = tmp_path / "sweep.db"
    w = World(db).setup()
    w.open(w.contract(hours=1))
    assert w.service.sweep(now=datetime.now()) == []  # not due yet
    acted = w.service.sweep(now=datetime.now() + timedelta(hours=2))
    assert acted == [("deal-1", "voided")]
    assert w.paypal.calls == {"authorize": 1, "capture": 0, "void": 1}
    assert w.store.get_deal("deal-1")["state"] == "VOIDED"
    assert w.http.get("/v1/deals/deal-1").json()["state"] == "VOIDED"
    assert w.service.sweep(now=datetime.now() + timedelta(hours=3)) == []  # terminal deals drop out
    assert w.paypal.calls["void"] == 1


def test_lifespan_runs_and_stops_the_background_sweep(monkeypatch):
    monkeypatch.setenv("TRUSTEE_SWEEP_SECONDS", "0.05")
    w = World().setup()
    ticks: list[int] = []
    original = w.service.sweep
    w.service.sweep = lambda now=None: ticks.append(1) or original(now)  # type: ignore[method-assign]
    with TestClient(w.app) as client:
        deadline = time.monotonic() + 3
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.get("/healthz").status_code == 200
    assert ticks
