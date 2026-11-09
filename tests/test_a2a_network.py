"""Northwind and Tidewater as real A2A agents: cards, a whole purchase over A2A, forged and altered moves,
the cheat variant, the payer-approval wait, and the arena `a2a` scenario.

No real network. All three agents run inside one FastAPI app (the arena app, which mounts Northwind and Tidewater
under /agents/*), and every call between them is a real HTTP request through one TestClient. PayPal is the dry-run
client, or the mocked sandbox for the approval tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_approval import FakePayPal
from test_arena_platform import Clock, approve_on_tick, make_runner, tape
from test_platform_api import CountingPayPal

from trustee.a2a import A2AClient
from trustee.a2a.client import artifact_data
from trustee.a2a.models import Task, TaskState
from trustee.agents.buyer import ArtifactStore
from trustee.arena.api import create_app
from trustee.arena.fixtures import TRANSCRIPTS
from trustee.arena.rows import to_row
from trustee.arena.runner import default_checkers
from trustee.arena.scenario import FIXTURES, get_scenario
from trustee.arena.scripted import ScriptedLLM
from trustee.identity import AgentKey
from trustee.ledger import Ledger
from trustee.network import Hop, Northwind, Tidewater, Waiter
from trustee.network.app import NORTHWIND_MOUNT, TIDEWATER_MOUNT
from trustee.network.common import SIGNING_EXTENSION_URI, key_from_card
from trustee.paypal import PayPalClient
from trustee.platform import DealService
from trustee.protocol import Accept, Contract, Offer
from trustee.store import Store

BASE = "http://127.0.0.1:8000"  # a loopback host, which is what Trustee's push validation accepts over http
SC = get_scenario("a2a")
DEAL = "deal-a2a-1"


def fast_waiter(timeout: float = 15.0) -> Waiter:
    return Waiter(poll_seconds=0.01, timeout_seconds=timeout)


class Net:
    """Trustee, Northwind and Tidewater in one app, talking to each other over HTTP."""

    def __init__(self, paypal: Any = None, tmp: Path | None = None):
        self.paypal = paypal if paypal is not None else CountingPayPal()
        self.service = DealService(Store(":memory:"), self.paypal, Ledger(), operator_token=None)
        self.app = create_app(ledger=Ledger(), paypal=self.paypal, service=self.service)
        self.http = TestClient(self.app, base_url=BASE)
        self.app.state.a2a.push.http = self.http  # Trustee's push to Tidewater goes through the same app
        self.nw: Northwind = self.app.state.northwind
        self.tw: Tidewater = self.app.state.tidewater
        self.hops: list[Hop] = []
        self.tmp = tmp
        self.approvals: list[tuple[str, str, str]] = []

    def llm(self) -> ScriptedLLM:
        return ScriptedLLM.from_file(TRANSCRIPTS / "honest.json")

    @contextmanager
    def agents(self, *, cheat: bool = False, waiter: Waiter | None = None, **northwind: Any) -> Iterator[Net]:
        llm = self.llm()
        store = ArtifactStore.load(FIXTURES, inbox=self.tmp)
        seller = {
            "good": SC.good,
            "ask": SC.ask,
            "floor": SC.floor,
            "store": store,
            "llm": llm,
            "cheat": cheat,
            "trustee_url": BASE,
            "http": self.http,
            "public_url": BASE + TIDEWATER_MOUNT,
            "observer": self.hops.append,
        }
        buyer = {
            "good": SC.good,
            "budget": SC.budget,
            "llm": llm,
            "checkers": default_checkers(),
            "trustee_url": BASE,
            "http": self.http,
            "public_url": BASE + NORTHWIND_MOUNT,
            "waiter": waiter or fast_waiter(),
            "inbox_root": self.tmp,
            "observer": self.hops.append,
            "on_approval": lambda *a: self.approvals.append(a),
            **northwind,
        }
        with self.tw.session(**seller), self.nw.session(**buyer):
            yield self

    def flush(self) -> None:
        assert self.app.state.a2a.push.flush(10)

    def deal(self, deal_id: str = DEAL) -> dict[str, Any]:
        out: dict[str, Any] = self.http.get(f"/v1/deals/{deal_id}").json()
        return out

    def hops_of(self, caller: str | None = None, method: str | None = None, skill: str | None = None) -> list[Hop]:
        return [
            h
            for h in self.hops
            if (caller is None or h.caller.startswith(caller))
            and (method is None or h.method == method)
            and (skill is None or h.skill == skill)
        ]


@pytest.fixture
def net(tmp_path: Path) -> Net:
    return Net(tmp=tmp_path)


def card(net: Net, mount: str, name: str = "agent-card.json") -> dict[str, Any]:
    resp = net.http.get(f"{mount}/.well-known/{name}")
    assert resp.status_code == 200
    out: dict[str, Any] = resp.json()
    return out


# ------------------------------------------------------------------------------------------ cards


@pytest.mark.parametrize("mount", [NORTHWIND_MOUNT, TIDEWATER_MOUNT])
def test_each_agent_serves_its_own_card_at_its_mount(net: Net, mount: str) -> None:
    for name in ("agent-card.json", "agent.json"):
        c = card(net, mount, name)
        first = c["supportedInterfaces"][0]
        assert first == {"url": f"{BASE}{mount}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        assert c["url"] == f"{BASE}{mount}/a2a" and c["preferredTransport"] == "JSONRPC"
        assert c["capabilities"]["streaming"] is False
    # the trustee card at the root is a different agent
    assert net.http.get("/.well-known/agent-card.json").json()["name"] == "Trustee"


def test_cards_list_skills_and_publish_the_signing_key(net: Net) -> None:
    tw = card(net, TIDEWATER_MOUNT)
    nw = card(net, NORTHWIND_MOUNT)
    assert {s["id"] for s in tw["skills"]} == {"quote", "negotiate", "sign-contract", "escrow-opened"}
    assert {s["id"] for s in nw["skills"]} == {"receive-delivery"}
    for c, agent in ((tw, net.tw), (nw, net.nw)):
        ext = next(e for e in c["capabilities"]["extensions"] if e["uri"] == SIGNING_EXTENSION_URI)
        assert ext["params"] == {
            "algorithm": "ed25519",
            "agentId": agent.identity.agent_id,
            "principal": agent.identity.principal,
            "publicKey": agent.identity.public_key,
        }
        key = key_from_card(c, "x")
        assert key.agent_id == agent.identity.agent_id and key.public_key == agent.identity.public_key
    assert tw["name"] != nw["name"] and net.tw.identity.public_key != net.nw.identity.public_key


def test_a_generic_a2a_client_can_talk_to_an_agent_by_its_card(net: Net) -> None:
    with net.agents():
        client = A2AClient(BASE + TIDEWATER_MOUNT, http=net.http)
        assert client.endpoint() == f"{BASE}{TIDEWATER_MOUNT}/a2a"
        assert "negotiate" in client.skill_ids()
        quote = client.call_skill("quote", {})
        assert isinstance(quote, Task) and quote.status.state == TaskState.COMPLETED
        data = artifact_data(quote, "result")
        assert data is not None and data["ask"] == "180.00" and data["good"]["sha256"] == SC.good.sha256
        assert "floor" not in str(data)
        text = client.send_message("what can you do?")
        assert text.role.value == "ROLE_AGENT"  # type: ignore[union-attr]
        with pytest.raises(Exception, match="-32602"):
            client.call_skill("no-such-skill", {})


# ----------------------------------------------------------------------------------- a purchase


def test_full_purchase_over_a2a_settles_with_one_capture(net: Net) -> None:
    with net.agents():
        purchase = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id=DEAL)
        net.flush()

    assert purchase.outcome is None and purchase.settled
    assert purchase.price == Decimal("156.00") and purchase.contract is not None
    assert purchase.task is not None and purchase.task.status.state == TaskState.COMPLETED
    receipt = purchase.receipt
    assert receipt is not None and receipt["outcome"] == "settled" and receipt["amount"] == "156.00"
    assert receipt["contractDigest"] == purchase.contract.digest()
    assert net.paypal.calls == {"authorize": 1, "capture": 1, "void": 0}
    assert net.deal()["state"] == "SETTLED"
    # Trustee's own task, read over A2A, agrees
    trustee_task = A2AClient(BASE, http=net.http).get_task(DEAL)
    assert trustee_task.status.state == TaskState.COMPLETED
    assert artifact_data(trustee_task, "settlement-receipt") == receipt

    # both signatures are on the contract, and the key behind each is the one in that agent's card
    assert set(purchase.contract.signatures) == {net.nw.identity.agent_id, net.tw.identity.agent_id}

    # Tidewater learned "funds held" from Trustee's push, not by polling, and shipped because of it
    assert (DEAL, "INPUT_REQUIRED") in net.tw.pushes
    assert [d["trigger"] for d in net.tw.deliveries] == ["push"]
    tide_reads = net.hops_of("tidewater", "GetTask")
    assert len(tide_reads) == 1  # the one confirmation before it registered the webhook
    order = [(h.method, h.skill) for h in net.hops]
    assert order.index(("GetTask", "task")) < order.index(("CreateTaskPushNotificationConfig", "task"))
    assert order.index(("CreateTaskPushNotificationConfig", "task")) < order.index(("push", "task"))
    assert order.index(("push", "task")) < order.index(("SendMessage", "receive-delivery"))

    # Northwind's inbox holds the CSV, with the contract it belongs to
    item = net.nw.inbox[DEAL]
    assert item.path.read_bytes()[:8] == b"order_id"
    assert item.sha256 == SC.good.sha256 and item.contract_digest == purchase.contract.digest()
    assert item.sender == net.tw.identity.agent_id
    # the attestation was Northwind's own check of those bytes
    assert purchase.attest is not None and purchase.attest["verdict"] == "accept"
    assert purchase.attest["evidence"]["sha256_check"] == "match"
    # Tidewater handed Trustee the file with its signature, so Trustee checked the bytes itself
    assert [h.note for h in net.hops_of("tidewater", skill="upload-deliverable")] == ["file handed to Trustee (201)"]
    assert net.deal()["verification"]["passed"] is True


def test_every_hop_is_reported_in_order(net: Net) -> None:
    with net.agents():
        net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id=DEAL)
        net.flush()
    seen = [(h.caller.split("-")[0], h.peer, h.skill) for h in net.hops]
    assert seen[:3] == [
        ("northwind", "tidewater", "card"),
        ("northwind", "tidewater", "quote"),
        ("northwind", "tidewater", "negotiate"),
    ]
    negotiate = net.hops_of("northwind", skill="negotiate")
    assert [h.note for h in negotiate] == ["counter", "accept"]
    assert [h.price for h in negotiate] == ["168.00", "156.00"]
    skills = {h.skill for h in net.hops}
    assert {
        "sign-contract",
        "register-agent",
        "open-escrow",
        "escrow-opened",
        "receive-delivery",
        "attest-delivery",
    } <= skills
    assert any(h.caller == "trustee" and h.peer == "tidewater" for h in net.hops)  # the push


def test_a_second_purchase_reuses_the_registrations(net: Net) -> None:
    with net.agents():
        first = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id="deal-a2a-1")
        net.nw.llm = net.tw.llm = net.llm()  # a fresh recorded negotiation for the second deal
        second = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id="deal-a2a-2")
        net.flush()
    assert first.settled and second.settled
    assert net.paypal.calls["capture"] == 2
    assert len(net.hops_of("northwind", skill="register-agent")) == 1
    assert len(net.hops_of("tidewater", skill="register-agent")) == 1


# ----------------------------------------------------------------------- forged and altered moves


def call_tidewater(net: Net, skill: str, payload: dict[str, Any]) -> Task:
    result = A2AClient(BASE + TIDEWATER_MOUNT, http=net.http).call_skill(skill, payload)
    assert isinstance(result, Task)
    return result


def refusal_status(task: Task) -> int:
    assert task.status.state == TaskState.REJECTED
    data = next(p.data for p in (task.status.message.parts if task.status.message else []) if p.data)
    return int(data["status"])


def offer_by(key: AgentKey, sender: str, deal_id: str, price: str = "160.00") -> Offer:
    return Offer.new(sender, SC.good, Decimal(price), deal_id=deal_id).sign(key)


def negotiate_payload(net: Net, offer: Offer) -> dict[str, Any]:
    return {"move": offer.model_dump(mode="json"), "buyer_card": f"{BASE}{NORTHWIND_MOUNT}/.well-known/agent-card.json"}


def test_a_forged_offer_is_refused_before_trustee_is_called(net: Net) -> None:
    me = net.nw.identity.agent_id
    with net.agents():
        before = len(net.service.ledger.tail())
        forged = offer_by(AgentKey.generate(), me, "deal-forged")  # right name, wrong key
        assert refusal_status(call_tidewater(net, "negotiate", negotiate_payload(net, forged))) == 401
        unsigned = Offer.new(me, SC.good, Decimal("160.00"), deal_id="deal-forged")
        assert refusal_status(call_tidewater(net, "negotiate", negotiate_payload(net, unsigned))) == 401
        # an impostor naming itself differently but pointing at Northwind's card
        other = offer_by(AgentKey.generate(), "northwind-planner-impostor", "deal-forged")
        assert refusal_status(call_tidewater(net, "negotiate", negotiate_payload(net, other))) == 401
        # no card, no first contact
        stranger = offer_by(AgentKey.generate(), "stranger-agent", "deal-forged")
        assert refusal_status(call_tidewater(net, "negotiate", {"move": stranger.model_dump(mode="json")})) == 422

        assert net.paypal.total == 0
        assert len(net.service.ledger.tail()) == before and net.service.ledger.tail("deal-forged") == []
        assert not net.hops_of(skill="register-principal") and not net.hops_of(skill="open-escrow")
        assert "deal-forged" not in net.tw.deals


def test_the_pinned_key_cannot_be_swapped_after_first_contact(net: Net) -> None:
    me, key = net.nw.identity.agent_id, net.nw.identity.agent_key
    with net.agents():
        ok = call_tidewater(net, "negotiate", negotiate_payload(net, offer_by(key, me, "deal-pin")))
        assert ok.status.state == TaskState.COMPLETED
        swapped = offer_by(AgentKey.generate(), me, "deal-pin-2")
        assert refusal_status(call_tidewater(net, "negotiate", negotiate_payload(net, swapped))) == 401


def test_northwind_refuses_a_reply_not_signed_by_the_sellers_card_key(
    net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, impostor = net.tw.identity.agent_key, AgentKey.generate()

    class Liar:
        public_key_b64 = real.public_key_b64  # the card keeps showing the real key

        def sign(self, data: bytes) -> str:
            return impostor.sign(data)  # but replies are signed with another

    monkeypatch.setattr(net.tw.identity, "agent_key", Liar())
    with net.agents():
        with pytest.raises(Exception, match="not signed by the key"):
            net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id=DEAL)
    assert net.paypal.total == 0 and net.service.ledger.tail(DEAL) == []


def accepted_deal(net: Net, deal_id: str, price: str = "160.00") -> Contract:
    """Drive one negotiation by hand (Northwind's key is in the process) until Tidewater accepts `price`."""
    me, key = net.nw.identity.agent_id, net.nw.identity.agent_key
    reply = call_tidewater(net, "negotiate", negotiate_payload(net, offer_by(key, me, deal_id, price)))
    move = (artifact_data(reply, "result") or {})["move"]
    assert move["kind"] == "accept" and move["price"] == price
    now = datetime.now()
    return Contract(
        deal_id=deal_id,
        buyer=me,
        seller=net.tw.identity.agent_id,
        good=SC.good,
        price=Decimal(price),
        deadline=now + timedelta(hours=1),
        formed_at=now,
    ).sign(me, key)


def test_tidewater_countersigns_exactly_what_it_accepted(net: Net) -> None:
    with net.agents():
        contract = accepted_deal(net, "deal-sign")
        done = call_tidewater(net, "sign-contract", {"contract": contract.model_dump(mode="json")})
        assert done.status.state == TaskState.COMPLETED
        signed = Contract.model_validate((artifact_data(done, "result") or {})["contract"])
        assert signed.digest() == contract.digest()
        assert signed.verify_party(net.tw.identity.agent_id, _registry(net))
        assert net.hops_of("tidewater", skill="register-agent")  # it registered itself at Trustee on the way
        again = call_tidewater(net, "sign-contract", {"contract": contract.model_dump(mode="json")})
        assert again.status.state == TaskState.COMPLETED  # idempotent for the same terms


def _registry(net: Net) -> Any:
    return net.service.registry


@pytest.mark.parametrize(
    ("label", "change", "status"),
    [
        ("price lowered", lambda c: c.model_copy(update={"price": Decimal("150.00")}), 409),
        ("price raised", lambda c: c.model_copy(update={"price": Decimal("170.00")}), 409),
        (
            "different file",
            lambda c: c.model_copy(update={"good": c.good.model_copy(update={"sha256": "0" * 64})}),
            409,
        ),
        (
            "different criteria",
            lambda c: c.model_copy(update={"good": c.good.model_copy(update={"acceptance_criteria": ["x"]})}),
            409,
        ),
        ("other seller", lambda c: c.model_copy(update={"seller": "someone-else"}), 409),
        ("expired", lambda c: c.model_copy(update={"deadline": datetime.now() - timedelta(minutes=1)}), 409),
        ("far future", lambda c: c.model_copy(update={"deadline": datetime.now() + timedelta(days=90)}), 409),
    ],
)
def test_tidewater_refuses_a_contract_whose_terms_differ(net: Net, label: str, change: Any, status: int) -> None:
    me, key = net.nw.identity.agent_id, net.nw.identity.agent_key
    with net.agents():
        agreed = accepted_deal(net, "deal-alter")
        altered = change(agreed).model_copy(update={"signatures": {}}).sign(me, key)  # validly signed, wrong terms
        refused = call_tidewater(net, "sign-contract", {"contract": altered.model_dump(mode="json")})
        assert refusal_status(refused) == status, label
        assert net.tw.deals["deal-alter"].signed is None
        assert net.paypal.total == 0 and not net.hops_of("tidewater", skill="register-agent")


def test_tidewater_refuses_to_sign_without_an_accepted_price_or_a_valid_buyer_signature(net: Net) -> None:
    me, key = net.nw.identity.agent_id, net.nw.identity.agent_key
    with net.agents():
        now = datetime.now()
        stray = Contract(
            deal_id="deal-never-negotiated",
            buyer=me,
            seller=net.tw.identity.agent_id,
            good=SC.good,
            price=Decimal("160.00"),
            deadline=now + timedelta(hours=1),
            formed_at=now,
        ).sign(me, key)
        assert refusal_status(call_tidewater(net, "sign-contract", {"contract": stray.model_dump(mode="json")})) == 409

        agreed = accepted_deal(net, "deal-badsig")
        unsigned = agreed.model_copy(update={"signatures": {}})
        assert (
            refusal_status(call_tidewater(net, "sign-contract", {"contract": unsigned.model_dump(mode="json")})) == 401
        )
        forged = agreed.model_copy(update={"signatures": {}}).sign(me, AgentKey.generate())
        assert refusal_status(call_tidewater(net, "sign-contract", {"contract": forged.model_dump(mode="json")})) == 401
        assert net.paypal.total == 0


def test_tidewater_never_sells_below_its_floor_even_if_the_model_says_accept(net: Net) -> None:
    me, key = net.nw.identity.agent_id, net.nw.identity.agent_key

    class Yes(ScriptedLLM):
        def complete(self, system: str, messages: Any) -> str:  # type: ignore[override]
            return '{"kind": "accept", "price": "1.00"}'

    with net.agents():
        net.tw.llm = Yes()
        reply = call_tidewater(net, "negotiate", negotiate_payload(net, offer_by(key, me, "deal-floor", "10.00")))
        move = (artifact_data(reply, "result") or {})["move"]
        assert move["kind"] == "counter" and Decimal(move["price"]) == SC.floor


def test_an_accept_must_answer_the_price_tidewater_quoted(net: Net) -> None:
    me, key = net.nw.identity.agent_id, net.nw.identity.agent_key
    with net.agents():
        first = call_tidewater(net, "negotiate", negotiate_payload(net, offer_by(key, me, "deal-acc", "100.00")))
        counter = (artifact_data(first, "result") or {})["move"]
        cheap = Accept(
            sender=me,
            responds_to=counter["msg_id"],
            good=SC.good,
            price=Decimal("100.00"),
            deadline=datetime.now() + timedelta(hours=1),
            deal_id="deal-acc",
        ).sign(key)
        assert refusal_status(call_tidewater(net, "negotiate", negotiate_payload(net, cheap))) == 409  # type: ignore[arg-type]


# --------------------------------------------------------------------------------- delivery side


def test_northwind_accepts_a_delivery_only_for_a_deal_it_signed_from_its_seller(net: Net) -> None:
    client = A2AClient(BASE + NORTHWIND_MOUNT, http=net.http)
    with net.agents():
        net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id=DEAL)
        net.flush()
        item = net.nw.inbox[DEAL]
        # a replay for a finished deal, an unknown deal, and a forged delivery
        for payload in (
            {"delivery": item.delivery.model_dump(mode="json"), "contract_digest": item.contract_digest},
            {"delivery": item.delivery.model_copy(update={"deal_id": "deal-nope"}).model_dump(mode="json")},
        ):
            reply = client.call_skill("receive-delivery", payload)
            assert isinstance(reply, Task) and reply.status.state == TaskState.REJECTED
        assert item.path.read_bytes()[:8] == b"order_id"


# ------------------------------------------------------------------------------------- cheating


def test_a_cheating_tidewater_is_refunded_and_nothing_is_captured(net: Net) -> None:
    with net.agents(cheat=True):
        purchase = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id="deal-cheat")
        net.flush()
    assert purchase.outcome is None and not purchase.settled
    assert purchase.attest is not None and purchase.attest["verdict"] == "reject"
    assert purchase.attest["evidence"]["sha256_check"] == "mismatch"
    receipt = purchase.receipt
    assert receipt is not None and receipt["outcome"] == "refunded" and "captureId" not in receipt
    assert purchase.task is not None and purchase.task.status.state == TaskState.COMPLETED
    assert net.paypal.calls == {"authorize": 1, "capture": 0, "void": 1}
    assert net.deal("deal-cheat")["state"] == "VOIDED"
    assert net.nw.inbox["deal-cheat"].sha256 != SC.good.sha256  # the junk twin really arrived


# ------------------------------------------------------------------------------- payer approval


def approval_net(tmp: Path) -> tuple[Net, FakePayPal]:
    fake = FakePayPal()
    return Net(paypal=fake.client(), tmp=tmp), fake


def test_the_approve_url_is_surfaced_and_a_later_approval_releases_the_push(tmp_path: Path) -> None:
    net, fake = approval_net(tmp_path)

    def approve_on_second_tick() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.order_status = "APPROVED"
            net.http.get("/v1/paypal/return", params={"token": "ORD-1"})

    calls = {"n": 0}
    with net.agents(poll_hook=approve_on_second_tick):
        purchase = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id=DEAL)
        net.flush()
    assert net.approvals and net.approvals[0][0] == DEAL and net.approvals[0][2].endswith("token=ORD-1")
    assert purchase.settled
    assert len(fake.calls("/capture")) == 1 and len(fake.calls("/authorize")) == 1
    # Tidewater registered while the task still asked for approval, and was pushed INPUT_REQUIRED after it
    assert (DEAL, "INPUT_REQUIRED") in net.tw.pushes
    assert [d["trigger"] for d in net.tw.deliveries] == ["push"]


def test_no_payer_approval_cancels_the_task_and_holds_nothing(tmp_path: Path) -> None:
    net, fake = approval_net(tmp_path)
    with net.agents(waiter=fast_waiter(timeout=0.2)):
        purchase = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id=DEAL)
        net.flush()
    assert purchase.outcome == "approval_timeout"
    assert purchase.task is not None and purchase.task.status.state == TaskState.CANCELED
    assert fake.money_calls == 0 and net.tw.deliveries == []


# ----------------------------------------------------------------------------- refusals at Trustee


def test_a_mandate_refusal_at_trustee_is_an_outcome_not_a_crash(net: Net) -> None:
    with net.agents(mandate_ceiling=Decimal("100.00")):  # Northwind's own mandate is below the agreed price
        purchase = net.nw.buy(BASE + TIDEWATER_MOUNT, deal_id="deal-mandate")
    assert purchase.outcome == "rejected_mandate" and purchase.receipt is None
    assert net.paypal.total == 0 and net.tw.deliveries == []


# ------------------------------------------------------------------------------------- the arena


def arena(paypal: PayPalClient | None = None) -> tuple[Any, TestClient]:
    holder: dict[str, TestClient] = {}
    app = create_app(ledger=Ledger(), paypal=paypal or PayPalClient(dry_run=True), self_client=lambda: holder["http"])
    holder["http"] = TestClient(app, base_url=BASE)
    app.state.a2a.push.http = holder["http"]
    return app, holder["http"]


def test_scenario_a2a_settles_with_a2a_rows_on_the_tape() -> None:
    app, _http = arena()
    runner = app.state.runner
    result = runner.run(get_scenario("a2a"), mode="scripted")
    assert result.error is None and result.outcome is None
    assert result.state == "SETTLED" and result.price == "156.00" and result.effective_mode == "scripted"
    assert result.attest is not None and result.attest["verdict"] == "accept"

    rows = [to_row(e, i) for i, e in enumerate(runner.ledger.tail(result.deal_id))]
    a2a = [r for r in rows if r["kind"] == "a2a"]
    events = [r["event"] for r in a2a]
    assert "tidewater GetAgentCard card" in events and "tidewater SendMessage quote" in events
    assert events.count("tidewater SendMessage negotiate") >= 2
    assert "trustee SendMessage open-escrow" in events and "trustee SendMessage attest-delivery" in events
    assert "northwind SendMessage receive-delivery" in events  # Tidewater -> Northwind, with the file
    push = next(r for r in a2a if r["event"] == "tidewater push task")
    assert push["actor"] == "trustee" and push["note"] == "INPUT_REQUIRED"
    counter = next(r for r in a2a if r["event"] == "tidewater SendMessage negotiate")
    assert (
        counter["actor"].startswith("northwind-planner")
        and counter["note"] == "counter"
        and counter["price"] == "168.00"
    )
    # Trustee's own rows for the deal are on the same tape
    assert any(r["kind"] == "paypal" and r["paypal_op"] == "capture" for r in rows)
    assert any(r["kind"] == "state" and r["state_to"] == "SETTLED" for r in rows)
    assert rows[-1]["event"] == "run_finished"


def test_the_arena_lists_the_a2a_scenario_and_the_other_scenarios_still_run() -> None:
    app, http = arena()
    names = [s["name"] for s in http.get("/api/scenarios").json()["scenarios"]]
    assert names == ["honest", "cheat", "forged", "outside", "a2a", "overreach"]
    info = next(s for s in http.get("/api/scenarios").json()["scenarios"] if s["name"] == "a2a")
    assert info["flow"] == "a2a" and info["title"] == "Three A2A agents"

    runner = app.state.runner
    assert runner.run(get_scenario("honest"), mode="scripted").state == "SETTLED"
    assert runner.run(get_scenario("cheat"), mode="scripted").state == "VOIDED"
    assert runner.run(get_scenario("forged"), mode="scripted").outcome == "rejected_identity"
    assert runner.run(get_scenario("outside"), mode="scripted").state == "SETTLED"


def test_a_cheating_seller_in_the_a2a_scenario_is_refunded(tmp_path: Path) -> None:
    app, _http = arena()
    runner = app.state.runner
    cheat = get_scenario("a2a").__class__(**{**get_scenario("a2a").__dict__, "cheat": True})
    result = runner.run(cheat, mode="scripted")
    assert result.state == "VOIDED" and result.attest is not None and result.attest["verdict"] == "reject"


def test_a2a_runs_are_serialized_on_the_shared_agents() -> None:
    app, _http = arena()
    runner = app.state.runner
    first = runner.run(get_scenario("a2a"), mode="scripted")
    second = runner.run(get_scenario("a2a"), mode="scripted")
    assert first.state == second.state == "SETTLED" and first.deal_id != second.deal_id
    # the run put the agents back the way it found them
    assert app.state.northwind.trustee_url is None and app.state.tidewater.trustee_url is None


# ------------------------------------------------------------------ PayPal says no for good


def refuse_authorize(fake: FakePayPal, status: int, count: int = 6) -> None:
    body = {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYEE_ACCOUNT_INVALID"}]}
    fake.fail["/authorize"] = [httpx.Response(status, json=body) for _ in range(count)]


def test_the_runner_stops_after_one_authorize_when_paypal_refuses_for_good() -> None:
    fake = FakePayPal()
    refuse_authorize(fake, 422)
    clock = Clock(on_tick=approve_on_tick(fake, 2))
    runner = make_runner(fake.client(), clock.wait())
    result = runner.run(get_scenario("honest"), mode="scripted")

    assert result.outcome == "approval_failed" and result.error is None and result.state == "VOIDED"
    assert len(fake.calls("/authorize")) == 1  # not one per 3-second tick
    assert fake.calls("/capture") == [] and fake.calls("/void") == []
    row = next(r for r in tape(runner, result.deal_id) if r["event"] == "approval_failed")
    assert "PAYEE_ACCOUNT_INVALID" in row["note"] and "Nothing was held" in row["note"]


def test_the_runner_still_retries_a_transient_paypal_failure() -> None:
    fake = FakePayPal()
    fake.fail["/authorize"] = [httpx.Response(503), httpx.Response(503), httpx.Response(429)]
    clock = Clock(on_tick=approve_on_tick(fake, 1))
    runner = make_runner(fake.client(), clock.wait())
    result = runner.run(get_scenario("honest"), mode="scripted")
    assert result.state == "SETTLED" and result.outcome is None
    assert len(fake.calls("/authorize")) >= 2


def test_the_a2a_flow_stops_asking_paypal_after_a_permanent_refusal() -> None:
    fake = FakePayPal()
    fake.order_status = "APPROVED"  # the payer already approved; PayPal then refuses to authorize
    refuse_authorize(fake, 422)
    app, _http = arena(fake.client())
    runner = app.state.runner
    result = runner.run(get_scenario("a2a"), mode="scripted")

    assert result.outcome == "approval_failed" and result.error is None
    assert len(fake.calls("/authorize")) == 1
    assert fake.calls("/capture") == [] and fake.calls("/void") == []
    assert result.state == "VOIDED"
    row = next(r for r in tape(runner, result.deal_id) if r["event"] == "approval_failed")
    assert "HTTP 422" in row["note"]
