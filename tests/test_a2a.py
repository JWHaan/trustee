"""The A2A JSON-RPC binding: discover Trustee, run a whole escrow, and be told when funds move.

No real network: HTTP runs through FastAPI's TestClient and push webhooks through httpx.MockTransport.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_approval import FakePayPal
from test_platform_api import GOOD_HASH, TOKEN, World

from trustee.a2a import A2AClient, A2AError, Task, TaskState
from trustee.a2a.client import artifact_data, last_refusal, push_token_matches, status_text, task_from_push
from trustee.a2a.models import AuthenticationInfo, Message, TaskPushNotificationConfig
from trustee.a2a.push import PushConfigError, validate_push_url
from trustee.a2a.skills import SKILLS
from trustee.identity import AgentKey
from trustee.mandate import Mandate
from trustee.platform.agent_card import build_agent_card
from trustee.platform.wire import agent_signing_bytes, mandate_to_dict, principal_signing_bytes
from trustee.protocol import Attest, Delivery

HOOK = "https://seller-agent.example.test/a2a-hook"
HOOK_TOKEN = "tok-seller-001"


class Hook:
    """A recording push-notification receiver behind httpx.MockTransport."""

    def __init__(self, statuses: list[int] | None = None, gate: threading.Event | None = None):
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])
        self.gate = gate

        def handler(req: httpx.Request) -> httpx.Response:
            if self.gate is not None:
                self.gate.wait(5)
            self.requests.append(req)
            return httpx.Response(self.statuses.pop(0) if self.statuses else 200)

        self.client = httpx.Client(transport=httpx.MockTransport(handler))

    def tasks(self) -> list[Task]:
        return [task_from_push(json.loads(r.content)) for r in self.requests]


class A2AWorld(World):
    """A World plus an A2AClient on the same in-process app."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.client = A2AClient("http://testserver", http=self.http, authorization=f"Bearer {TOKEN}")
        self.hook = Hook()
        self.app.state.a2a.push.http = self.hook.client

    def use_hook(self, hook: Hook) -> Hook:
        self.hook = hook
        self.app.state.a2a.push.http = hook.client
        return hook

    def flush(self) -> None:
        assert self.app.state.a2a.push.flush(5)

    # -- signed payloads, built the way an outside agent would
    def delivery(
        self, deal_id: str = "deal-1", signer: str = "seller", content_hash: str = GOOD_HASH
    ) -> dict[str, Any]:
        msg = Delivery(sender=signer, deal_id=deal_id, deliverable_ref="m.zip", content_hash=content_hash)
        return msg.sign(self.keys[signer]).model_dump(mode="json")

    def attestation(self, deal_id: str = "deal-1", verdict: str = "accept") -> dict[str, Any]:
        evidence = {"sha256_check": "mismatch"} if verdict == "reject" else {"sha256_check": "match"}
        msg = Attest(sender="buyer", deal_id=deal_id, verdict=verdict, reason="because", evidence=evidence)
        return msg.sign(self.keys["buyer"]).model_dump(mode="json")

    def contract_input(self, deal_id: str = "deal-1", price: str = "25.00") -> dict[str, Any]:
        return self.contract(deal_id, price).model_dump(mode="json")

    def register_over_a2a(self) -> None:
        """Principals, agents and mandates, every step through a skill: no REST call."""
        k = self.keys
        for name, key in (("org-b", k["org-b"]), ("org-s", k["org-s"])):
            pub = key.public_key_b64
            task = self.client.call_skill(
                "register-principal",
                {"principal": name, "public_key": pub, "proof": key.sign(principal_signing_bytes(name, pub))},
            )
            assert isinstance(task, Task) and task.status.state == TaskState.COMPLETED
        for agent_id, role, principal in (("buyer", "buyer", "org-b"), ("seller", "seller", "org-s")):
            key, pkey = k[agent_id], k[principal]
            body = agent_signing_bytes(agent_id, key.public_key_b64, principal, [role], None)
            task = self.client.call_skill(
                "register-agent",
                {
                    "agent_id": agent_id,
                    "public_key": key.public_key_b64,
                    "principal": principal,
                    "roles": [role],
                    "paypal_email": None,
                    "proof": key.sign(body),
                    "principal_signature": pkey.sign(body),
                },
            )
            assert isinstance(task, Task) and task.status.state == TaskState.COMPLETED
        from decimal import Decimal

        cats = frozenset({"code"})
        for agent_id, principal, grants, others in (
            ("buyer", "org-b", "spend", {"seller"}),
            ("seller", "org-s", "receive", {"buyer"}),
        ):
            mandate = Mandate(
                agent_id=agent_id,
                principal=principal,
                max_amount=Decimal("500.00"),
                allowed_categories=cats,
                allowed_counterparties=frozenset(others),
                grants=grants,  # type: ignore[arg-type]
            )
            task = self.client.call_skill("publish-mandate", mandate_to_dict(mandate.sign(k[principal])))
            assert isinstance(task, Task) and task.status.state == TaskState.COMPLETED


@pytest.fixture
def world() -> A2AWorld:
    return A2AWorld().setup()


def as_task(result: Task | Message) -> Task:
    assert isinstance(result, Task), result
    return result


def rpc(w: World, method: str, params: Any = None, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        body["params"] = params
    resp = w.http.post("/a2a", json=body, **kw)
    assert resp.status_code == 200
    out: dict[str, Any] = resp.json()
    return out


def user_message(parts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"messageId": "m-" + str(time.monotonic_ns()), "role": "ROLE_USER", "parts": parts, **extra}


def skill_message(skill: str, payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return user_message([{"data": {"skill": skill, "input": payload}}], **extra)


# ------------------------------------------------------------------------------------ discovery


def test_card_advertises_jsonrpc_and_push_notifications(world: A2AWorld) -> None:
    card = world.http.get("/.well-known/agent-card.json").json()
    first = card["supportedInterfaces"][0]
    assert first == {"url": "http://testserver/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert any(i["protocolBinding"] == "HTTP+JSON" and i["url"].endswith("/v1") for i in card["supportedInterfaces"])
    assert card["capabilities"]["pushNotifications"] is True and card["capabilities"]["streaming"] is False
    # v0.3 field names still point a legacy client at the same JSON-RPC endpoint
    assert card["url"] == "http://testserver/a2a" and card["preferredTransport"] == "JSONRPC"
    assert {"url": "http://testserver/v1", "transport": "HTTP+JSON"} in card["additionalInterfaces"]
    # 1.0 and v0.3 security shapes side by side
    assert card["securityRequirements"] == [{"schemes": {"ed25519Signature": {"list": []}}}]
    assert card["security"] == [{"ed25519Signature": []}]
    assert card["securitySchemes"]["operatorBearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert "/a2a" in card["description"]


def test_card_skills_match_the_skills_the_server_runs() -> None:
    advertised = {s["id"] for s in build_agent_card("http://x")["skills"]}
    assert advertised == set(SKILLS)
    assert "A2A: SendMessage" in build_agent_card("http://x")["skills"][0]["description"]


def test_client_discovers_the_jsonrpc_endpoint_from_the_card(world: A2AWorld) -> None:
    assert world.client.endpoint() == "http://testserver/a2a"
    assert "open-escrow" in world.client.skill_ids()
    legacy_card = A2AClient(card_url="http://testserver/.well-known/agent.json", http=world.http)
    assert legacy_card.endpoint() == "http://testserver/a2a"


def test_text_only_message_gets_a_help_message_not_a_task(world: A2AWorld) -> None:
    reply = world.client.send_message("what can you do?")
    assert isinstance(reply, Message)
    assert reply.role.value == "ROLE_AGENT" and reply.taskId is None
    text = status_text(Task(id="x", status={"state": TaskState.COMPLETED, "message": reply}))  # type: ignore[arg-type]
    assert "open-escrow" in text and '"skill"' in text
    data = next(p.data for p in reply.parts if p.data is not None)
    assert set(data["skills"]) == set(SKILLS)


# -------------------------------------------------------------------------------- full lifecycles


def test_full_honest_deal_over_a2a_only_settles_with_one_capture() -> None:
    w = A2AWorld()
    w.register_over_a2a()
    created = as_task(w.client.call_skill("open-escrow", w.contract_input()))
    assert created.id == "deal-1" and created.contextId == "deal-1"
    assert created.status.state == TaskState.INPUT_REQUIRED
    assert "record-delivery" in status_text(created)
    assert created.artifacts is None

    delivered = as_task(w.client.call_skill("record-delivery", w.delivery(), task_id="deal-1"))
    assert delivered.status.state == TaskState.WORKING and "attest-delivery" in status_text(delivered)

    done = as_task(w.client.call_skill("attest-delivery", w.attestation(), task_id="deal-1"))
    assert done.status.state == TaskState.COMPLETED
    receipt = artifact_data(done, "settlement-receipt")
    assert receipt is not None
    assert receipt["outcome"] == "settled" and receipt["amount"] == "25.00" and receipt["currency"] == "USD"
    assert receipt["captureId"].startswith("dry-cap-") and receipt["orderId"]
    assert receipt["contractDigest"] == w.contract().digest()
    assert receipt["closedBy"] == "buyer attested accept; funds captured"
    assert w.paypal.calls == {"authorize": 1, "capture": 1, "void": 0}

    fetched = w.client.get_task("deal-1")
    assert fetched.status.state == TaskState.COMPLETED and artifact_data(fetched, "settlement-receipt") == receipt
    assert w.http.get("/v1/deals/deal-1").json()["state"] == "SETTLED"  # same deal, seen over REST
    assert w.paypal.calls["capture"] == 1


def test_cheating_seller_is_refunded_and_the_task_still_completes(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    world.client.call_skill("record-delivery", world.delivery(content_hash="b" * 64), task_id="deal-1")
    done = as_task(world.client.call_skill("attest-delivery", world.attestation(verdict="reject"), task_id="deal-1"))
    assert done.status.state == TaskState.COMPLETED
    receipt = artifact_data(done, "settlement-receipt")
    assert receipt is not None and receipt["outcome"] == "refunded"
    assert receipt["ruling"]["decision"] == "release" and receipt["ruling"]["clause"]
    assert "captureId" not in receipt
    assert world.paypal.calls == {"authorize": 1, "capture": 0, "void": 1}


def test_open_escrow_is_idempotent_and_conflicting_terms_are_refused(world: A2AWorld) -> None:
    first = as_task(world.client.call_skill("open-escrow", world.contract_input()))
    again = as_task(world.client.call_skill("open-escrow", world.contract_input()))
    assert first.status.state == again.status.state == TaskState.INPUT_REQUIRED
    assert world.paypal.calls["authorize"] == 1
    other = as_task(world.client.call_skill("open-escrow", world.contract_input(price="30.00")))
    assert other.status.state == TaskState.REJECTED and other.id != "deal-1"
    assert "different terms" in status_text(other)


def test_forged_contract_is_rejected_and_never_touches_paypal(world: A2AWorld) -> None:
    attacker = AgentKey.generate()
    forged = world.contract("deal-f", sign=False).sign("buyer", attacker).sign("seller", world.keys["seller"])
    task = as_task(world.client.call_skill("open-escrow", forged.model_dump(mode="json")))
    assert task.status.state == TaskState.REJECTED
    assert "signature" in status_text(task).lower()
    assert world.paypal.total == 0
    with pytest.raises(A2AError) as exc:  # a refused contract never squats on the deal id
        world.client.get_task("deal-f")
    assert exc.value.code == -32001
    # the refusal itself is a task the client can fetch
    assert world.client.get_task(task.id).status.state == TaskState.REJECTED


def test_mandate_refusal_lists_violations(world: A2AWorld) -> None:
    task = as_task(world.client.call_skill("open-escrow", world.contract_input(price="900.00")))
    assert task.status.state == TaskState.REJECTED
    data = next(p.data for p in task.status.message.parts if p.data is not None)  # type: ignore[union-attr]
    assert data["status"] == 403 and data["violations"]
    assert world.paypal.total == 0


def test_refused_message_leaves_the_deal_task_in_its_true_state(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    wrong_signer = world.delivery(signer="buyer")  # only the seller may claim delivery
    task = as_task(world.client.call_skill("record-delivery", wrong_signer, task_id="deal-1"))
    assert task.status.state == TaskState.INPUT_REQUIRED  # still waiting for a valid delivery
    refusal = last_refusal(task)
    assert refusal is not None and refusal["status"] == 401
    assert "refused" in status_text(task)
    assert world.http.get("/v1/deals/deal-1").json()["state"] == "ESCROWED"


def test_registration_needs_the_operator_token_when_one_is_configured(world: A2AWorld) -> None:
    anonymous = A2AClient("http://testserver", http=world.http)
    key = AgentKey.generate()
    pub = key.public_key_b64
    body = {"principal": "org-x", "public_key": pub, "proof": key.sign(principal_signing_bytes("org-x", pub))}
    task = as_task(anonymous.call_skill("register-principal", body))
    assert task.status.state == TaskState.REJECTED and "operator" in status_text(task)


# ----------------------------------------------------------------------------- protocol errors


def test_unknown_method_is_32601(world: A2AWorld) -> None:
    out = rpc(world, "DoesNotExist", {})
    assert out["error"]["code"] == -32601 and out["id"] == 7 and "result" not in out


def test_bad_params_are_32602_with_field_violations(world: A2AWorld) -> None:
    out = rpc(world, "SendMessage", {"message": {"role": "ROLE_USER", "parts": []}})
    assert out["error"]["code"] == -32602
    fields = [v["field"] for v in out["error"]["data"][0]["fieldViolations"]]
    assert "message.messageId" in fields and "message.parts" in fields
    assert rpc(world, "GetTask", {})["error"]["code"] == -32602
    assert rpc(world, "GetTask", "not-an-object")["error"]["code"] == -32602
    bad_skill = rpc(world, "SendMessage", {"message": skill_message("no-such-skill", {})})
    assert bad_skill["error"]["code"] == -32602 and "open-escrow" in bad_skill["error"]["message"]
    bad_input = rpc(world, "SendMessage", {"message": skill_message("open-escrow", {"deal_id": "x"})})
    assert bad_input["error"]["code"] == -32602
    assert world.paypal.total == 0


def test_malformed_json_and_envelopes(world: A2AWorld) -> None:
    resp = world.http.post("/a2a", content=b"{nope", headers={"content-type": "application/json"})
    assert resp.json()["error"]["code"] == -32700 and resp.json()["id"] is None
    for body in (
        {"id": 1, "method": "GetTask"},
        {"jsonrpc": "2.0", "method": "GetTask"},
        [1, 2],
        {"jsonrpc": "2.0", "id": 1},
    ):
        assert world.http.post("/a2a", json=body).json()["error"]["code"] == -32600
    assert world.http.post("/a2a", content=b"x" * (1 << 20) + b"y").json()["error"]["code"] == -32600


def test_unknown_task_is_32001(world: A2AWorld) -> None:
    assert rpc(world, "GetTask", {"id": "nope"})["error"]["code"] == -32001
    assert rpc(world, "CancelTask", {"id": "nope"})["error"]["code"] == -32001
    out = rpc(world, "SendMessage", {"message": skill_message("get-deal", {"deal_id": "nope"})})
    assert out["error"]["code"] == -32001
    attest = rpc(world, "SendMessage", {"message": skill_message("attest-delivery", world.attestation("nope"))})
    assert attest["error"]["code"] == -32001


def test_task_addressing_is_validated_before_anything_runs(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    mismatch = rpc(
        world, "SendMessage", {"message": skill_message("record-delivery", world.delivery(), taskId="other")}
    )
    assert mismatch["error"]["code"] == -32001  # no such task
    world.client.call_skill("open-escrow", world.contract_input("deal-2"))
    crossed = rpc(
        world, "SendMessage", {"message": skill_message("record-delivery", world.delivery("deal-1"), taskId="deal-2")}
    )
    assert crossed["error"]["code"] == -32602
    bad_context = rpc(
        world,
        "SendMessage",
        {"message": skill_message("record-delivery", world.delivery(), taskId="deal-1", contextId="c")},
    )
    assert bad_context["error"]["code"] == -32602
    assert world.http.get("/v1/deals/deal-1").json()["state"] == "ESCROWED"  # none of the above ran


def test_messages_to_a_finished_task_are_unsupported_operation(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    world.client.call_skill("record-delivery", world.delivery(), task_id="deal-1")
    world.client.call_skill("attest-delivery", world.attestation(), task_id="deal-1")
    out = rpc(world, "SendMessage", {"message": skill_message("attest-delivery", world.attestation(), taskId="deal-1")})
    assert out["error"]["code"] == -32004
    # Addressed by the signed payload alone, an attest replay stays the same harmless no-op as over REST.
    replay = as_task(world.client.call_skill("attest-delivery", world.attestation()))
    assert replay.status.state == TaskState.COMPLETED and world.paypal.calls["capture"] == 1


def test_streaming_and_extended_card_are_unsupported(world: A2AWorld) -> None:
    for method in ("SendStreamingMessage", "SubscribeToTask", "GetExtendedAgentCard", "message/stream"):
        assert rpc(world, method, {"id": "x"})["error"]["code"] == -32004
    assert rpc(world, "GetTask", {"id": "x"}, headers={"A2A-Version": "9.9"})["error"]["code"] == -32009
    assert rpc(world, "GetTask", {"id": "x"}, headers={"A2A-Version": "0.3"})["error"]["code"] == -32001


def test_dispatch_never_leaks_internal_errors(world: A2AWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(world.service, "record", boom)
    resp = rpc(world, "GetTask", {"id": "deal-1"})
    assert resp["error"]["code"] == -32603 and "secret" not in json.dumps(resp)


# ------------------------------------------------------------------------------------ CancelTask


def test_cancel_on_an_escrowed_task_is_not_cancelable(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    out = rpc(world, "CancelTask", {"id": "deal-1"})
    assert out["error"]["code"] == -32002 and "funds are held" in out["error"]["message"]
    assert world.http.get("/v1/deals/deal-1").json()["state"] == "ESCROWED"
    assert world.paypal.total == 1  # the authorize at open, nothing since


def test_cancel_on_a_finished_task_is_not_cancelable(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    world.client.call_skill("record-delivery", world.delivery(), task_id="deal-1")
    world.client.call_skill("attest-delivery", world.attestation(), task_id="deal-1")
    assert rpc(world, "CancelTask", {"id": "deal-1"})["error"]["code"] == -32002


def awaiting_approval_world() -> tuple[A2AWorld, FakePayPal]:
    fake = FakePayPal()
    w = A2AWorld.__new__(A2AWorld)
    World.__init__(w, paypal=fake.client())  # type: ignore[arg-type]
    w.client = A2AClient("http://testserver", http=w.http, authorization=f"Bearer {TOKEN}")
    w.hook = Hook()
    w.app.state.a2a.push.http = w.hook.client
    w.setup()
    return w, fake


def test_cancel_before_payer_approval_cancels_without_a_paypal_call() -> None:
    w, fake = awaiting_approval_world()
    task = as_task(w.client.call_skill("open-escrow", w.contract_input()))
    assert task.status.state == TaskState.AUTH_REQUIRED
    approve_url = task.metadata["trustee"]["approveUrl"]  # type: ignore[index]
    assert approve_url.endswith("token=ORD-1") and approve_url in status_text(task)
    before = len(fake.requests)
    canceled = w.client.cancel_task("deal-1")
    assert canceled.status.state == TaskState.CANCELED
    assert len(fake.requests) == before and fake.money_calls == 0  # nothing was held, nothing was called
    assert w.http.get("/v1/deals/deal-1").json()["state"] == "VOIDED"
    assert w.client.get_task("deal-1").status.state == TaskState.CANCELED
    assert rpc(w, "CancelTask", {"id": "deal-1"})["error"]["code"] == -32002  # already canceled


def test_payer_approval_moves_the_task_on_and_blocks_a_late_cancel() -> None:
    w, fake = awaiting_approval_world()
    w.client.call_skill("open-escrow", w.contract_input())
    w.http.get("/v1/paypal/return", params={"token": "ORD-1"})
    task = w.client.get_task("deal-1")
    assert task.status.state == TaskState.INPUT_REQUIRED
    assert rpc(w, "CancelTask", {"id": "deal-1"})["error"]["code"] == -32002
    assert len(fake.calls("/authorize")) == 1


def test_paypal_failure_on_open_escrow_is_a_failed_retryable_task(monkeypatch: pytest.MonkeyPatch) -> None:
    w = A2AWorld().setup()
    w.paypal.fail_authorize = 1
    failed = as_task(w.client.call_skill("open-escrow", w.contract_input()))
    assert failed.status.state == TaskState.FAILED and failed.id == "deal-1"
    assert failed.metadata["trustee"]["retryable"] is True  # type: ignore[index]
    assert last_refusal(failed) is not None and last_refusal(failed)["status"] == 502  # type: ignore[index]
    retried = as_task(w.client.call_skill("open-escrow", w.contract_input()))  # same message, no taskId
    assert retried.status.state == TaskState.INPUT_REQUIRED
    assert w.paypal.calls["authorize"] == 2


# ----------------------------------------------------------------------------------- ListTasks


def test_list_tasks_filters_pages_and_orders_by_recent_update() -> None:
    w = A2AWorld().setup()
    for n in range(1, 4):
        w.client.call_skill("open-escrow", w.contract_input(f"deal-{n}"))
    w.client.call_skill("record-delivery", w.delivery("deal-1"), task_id="deal-1")  # deal-1 is now the freshest
    page = w.client.list_tasks(page_size=2)
    assert [t.id for t in page["tasks"]] == ["deal-1", "deal-3"] and page["totalSize"] == 3 and page["pageSize"] == 2
    rest = w.client.list_tasks(page_size=2, page_token=page["nextPageToken"])
    assert [t.id for t in rest["tasks"]] == ["deal-2"] and rest["nextPageToken"] == ""
    waiting = w.client.list_tasks(status=TaskState.INPUT_REQUIRED)
    assert {t.id for t in waiting["tasks"]} == {"deal-2", "deal-3"}
    assert w.client.list_tasks(context_id="deal-2")["totalSize"] == 1
    assert all(t.artifacts is None for t in page["tasks"])
    assert rpc(w, "ListTasks", {"pageSize": 101}, headers=_auth())["error"]["code"] == -32602
    assert rpc(w, "ListTasks", {"pageToken": "garbage"}, headers=_auth())["error"]["code"] == -32602
    assert rpc(w, "ListTasks", {"statusTimestampAfter": "yesterday-ish"}, headers=_auth())["error"]["code"] == -32602
    future = rpc(w, "ListTasks", {"statusTimestampAfter": "2999-01-01T00:00:00Z"}, headers=_auth())
    assert future["result"]["tasks"] == [] and future["result"]["nextPageToken"] == ""


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_listing_every_task_needs_the_operator_token_but_one_context_does_not(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    anonymous = A2AClient("http://testserver", http=world.http)
    with pytest.raises(A2AError) as exc:
        anonymous.list_tasks()
    assert exc.value.code == -32050
    assert anonymous.list_tasks(context_id="deal-1")["totalSize"] == 1
    assert world.client.list_tasks()["totalSize"] == 1  # the fixture client carries the operator token


def test_list_tasks_includes_artifacts_only_when_asked(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    world.client.call_skill("record-delivery", world.delivery(), task_id="deal-1")
    world.client.call_skill("attest-delivery", world.attestation(), task_id="deal-1")
    assert world.client.list_tasks()["tasks"][0].artifacts is None
    full = world.client.list_tasks(include_artifacts=True)["tasks"][0]
    assert artifact_data(full, "settlement-receipt")["outcome"] == "settled"  # type: ignore[index]
    raw = rpc(world, "ListTasks", {}, headers=_auth())["result"]["tasks"][0]
    assert "artifacts" not in raw  # omitted, not null or empty


# ----------------------------------------------------------------------------------- v0.3 aliases


def test_v03_aliases_answer_in_the_v03_shape(world: A2AWorld) -> None:
    params = {
        "message": {
            "kind": "message",
            "messageId": "m1",
            "role": "user",
            "parts": [{"kind": "data", "data": {"skill": "open-escrow", "input": world.contract_input()}}],
        },
        "configuration": {"blocking": True},
    }
    sent = rpc(world, "message/send", params)["result"]
    assert sent["kind"] == "task" and sent["id"] == "deal-1" and sent["status"]["state"] == "input-required"
    assert sent["status"]["message"]["kind"] == "message" and sent["status"]["message"]["role"] == "agent"
    assert all(p["kind"] in ("text", "data") for p in sent["status"]["message"]["parts"])

    got = rpc(world, "tasks/get", {"id": "deal-1"})["result"]
    assert got["status"]["state"] == "input-required"

    text_only = rpc(
        world,
        "message/send",
        {"message": {"messageId": "m2", "role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    )
    assert text_only["result"]["kind"] == "message" and text_only["result"]["parts"][0]["kind"] == "text"

    world.client.call_skill("record-delivery", world.delivery(), task_id="deal-1")
    world.client.call_skill("attest-delivery", world.attestation(), task_id="deal-1")
    done = rpc(world, "tasks/get", {"id": "deal-1"})["result"]
    assert done["status"]["state"] == "completed" and done["artifacts"][0]["parts"][0]["kind"] == "data"


def test_v03_cancel_and_push_config_methods() -> None:
    w, _fake = awaiting_approval_world()
    w.client.call_skill("open-escrow", w.contract_input())
    set_out = rpc(
        w,
        "tasks/pushNotificationConfig/set",
        {
            "taskId": "deal-1",
            "pushNotificationConfig": {
                "id": "c1",
                "url": HOOK,
                "token": HOOK_TOKEN,
                "authentication": {"schemes": ["Bearer"], "credentials": "cred"},
            },
        },
    )["result"]
    assert set_out["taskId"] == "deal-1" and set_out["pushNotificationConfig"]["id"] == "c1"
    assert set_out["pushNotificationConfig"]["authentication"] == {"schemes": ["Bearer"], "credentials": None}
    assert "token" not in set_out["pushNotificationConfig"]
    got = rpc(w, "tasks/pushNotificationConfig/get", {"id": "deal-1", "pushNotificationConfigId": "c1"})["result"]
    assert got["pushNotificationConfig"]["url"] == HOOK
    listed = rpc(w, "tasks/pushNotificationConfig/list", {"id": "deal-1"})["result"]
    assert [c["pushNotificationConfig"]["id"] for c in listed] == ["c1"]

    canceled = rpc(w, "tasks/cancel", {"id": "deal-1"})["result"]
    assert canceled["kind"] == "task" and canceled["status"]["state"] == "canceled"
    w.flush()
    # a v0.3 receiver is sent the bare v0.3 Task
    assert w.hook.requests, "the cancel transition should have been pushed"
    body = json.loads(w.hook.requests[-1].content)
    assert body["kind"] == "task" and body["status"]["state"] == "canceled" and "task" not in body

    assert (
        rpc(w, "tasks/pushNotificationConfig/delete", {"id": "deal-1", "pushNotificationConfigId": "c1"})["result"]
        is None
    )
    assert rpc(w, "tasks/pushNotificationConfig/list", {"id": "deal-1"})["result"] == []


# ------------------------------------------------------------------------- push notifications


def test_push_config_methods_round_trip_and_hide_secrets(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    auth = AuthenticationInfo(scheme="Bearer", credentials="very-secret")
    created = world.client.set_push_config("deal-1", HOOK, token=HOOK_TOKEN, authentication=auth, config_id="cfg-1")
    assert created.id == "cfg-1" and created.taskId == "deal-1" and created.url == HOOK
    assert created.token is None and created.authentication == AuthenticationInfo(scheme="Bearer")
    assert world.client.get_push_config("deal-1", "cfg-1").url == HOOK
    assert [c.id for c in world.client.list_push_configs("deal-1")] == ["cfg-1"]
    world.client.delete_push_config("deal-1", "cfg-1")
    world.client.delete_push_config("deal-1", "cfg-1")  # idempotent
    assert world.client.list_push_configs("deal-1") == []
    with pytest.raises(A2AError) as exc:
        world.client.get_push_config("deal-1", "cfg-1")
    assert exc.value.code == -32001
    with pytest.raises(A2AError) as exc2:
        world.client.set_push_config("no-such-deal", HOOK)
    assert exc2.value.code == -32001


def test_push_urls_must_be_https_except_for_local_agents(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    refused = [
        "http://agent.example.test/hook",
        "ftp://agent.example.test/hook",
        "https://10.0.0.5/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pw@agent.example.test/hook",
        "not a url",
    ]
    for url in refused:
        with pytest.raises(A2AError) as exc:
            world.client.set_push_config("deal-1", url)
        assert exc.value.code == -32602, url
        with pytest.raises(PushConfigError):
            validate_push_url(url)
    for ok in ("https://agent.example.test/hook", "http://127.0.0.1:9000/hook", "http://localhost:9000/h"):
        assert validate_push_url(ok) == ok
    assert world.client.list_push_configs("deal-1") == []


def test_push_config_limits_and_secret_hygiene(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    for n in range(5):
        world.client.set_push_config("deal-1", HOOK, config_id=f"c{n}")
    with pytest.raises(A2AError) as exc:
        world.client.set_push_config("deal-1", HOOK, config_id="c-extra")
    assert exc.value.code == -32602 and "at most" in exc.value.message
    with pytest.raises(A2AError) as dup:  # an existing id cannot be replaced by a create
        world.client.set_push_config("deal-1", "https://evil.example.test/steal", config_id="c0")
    assert dup.value.code == -32602 and "already exists" in dup.value.message
    assert world.client.get_push_config("deal-1", "c0").url == HOOK
    for bad in ({"token": "line\nbreak"}, {"authentication": {"scheme": "Bad Scheme", "credentials": "x"}}):
        out = rpc(world, "CreateTaskPushNotificationConfig", {"taskId": "deal-1", "url": HOOK, "id": "c0", **bad})
        assert out["error"]["code"] == -32602


def test_state_changes_are_pushed_to_the_seller_with_its_token() -> None:
    """The Tidewater story: the seller registers a webhook and is told when funds are held, with no polling."""
    w, _fake = awaiting_approval_world()
    cfg = TaskPushNotificationConfig(
        url=HOOK, token=HOOK_TOKEN, authentication=AuthenticationInfo(scheme="Bearer", credentials="cred-1")
    )
    task = as_task(
        w.client.send_message(
            Message.model_validate(skill_message("open-escrow", w.contract_input())),
            configuration={"taskPushNotificationConfig": cfg.dump()},
        )
    )
    assert task.status.state == TaskState.AUTH_REQUIRED
    assert task.metadata["trustee"]["pushNotification"]["registered"] is True  # type: ignore[index]
    assert len(w.client.list_push_configs("deal-1")) == 1

    w.http.get("/v1/paypal/return", params={"token": "ORD-1"})  # the payer approves at PayPal: funds are now held
    w.flush()
    pushed = w.hook.tasks()
    assert [t.status.state for t in pushed] == [TaskState.INPUT_REQUIRED]
    req = w.hook.requests[0]
    assert str(req.url) == HOOK and req.method == "POST"
    assert req.headers["content-type"] == "application/a2a+json"
    assert req.headers["x-a2a-notification-token"] == HOOK_TOKEN and push_token_matches(req.headers, HOOK_TOKEN)
    assert not push_token_matches(req.headers, "someone-else")
    assert req.headers["authorization"] == "Bearer cred-1"
    body = json.loads(req.content)
    assert set(body) == {"task"} and body["task"]["id"] == "deal-1"
    assert "record-delivery" in status_text(pushed[0])

    w.client.call_skill("record-delivery", w.delivery(), task_id="deal-1")
    w.client.call_skill("attest-delivery", w.attestation(), task_id="deal-1")
    w.flush()
    states = [t.status.state for t in w.hook.tasks()]
    assert states[0] == TaskState.INPUT_REQUIRED and states[-1] == TaskState.COMPLETED
    assert artifact_data(w.hook.tasks()[-1], "settlement-receipt")["outcome"] == "settled"  # type: ignore[index]
    assert states.count(TaskState.COMPLETED) == 1


def test_push_never_blocks_the_ledger_and_survives_a_dead_receiver(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    gate = threading.Event()
    world.use_hook(Hook(gate=gate))
    world.client.set_push_config("deal-1", HOOK, token=HOOK_TOKEN)
    started = time.monotonic()
    for _ in range(20):  # the receiver is stalled the whole time
        world.service.ledger.append("deal-1", "trustee", "state", event="transition", state_to="DELIVERED")
    assert time.monotonic() - started < 1.0
    gate.set()
    world.flush()
    assert len(world.hook.requests) <= 2  # 20 transitions collapse into at most a couple of notifications

    dead = Hook(statuses=[500, 500, 500, 500])
    world.use_hook(dead)
    world.app.state.a2a.push._sleep = lambda _s: None
    world.service.ledger.append("deal-1", "trustee", "state", event="transition", state_to="X")
    world.flush()  # four failures are retried then dropped; nothing raises into the ledger


def test_push_skips_deals_without_configs_and_unchanged_states(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input("deal-1"))
    world.client.call_skill("open-escrow", world.contract_input("deal-2"))
    world.client.set_push_config("deal-1", HOOK)
    world.client.call_skill("record-delivery", world.delivery("deal-2"), task_id="deal-2")
    world.flush()
    assert world.hook.requests == []  # deal-2 has no webhook
    world.client.call_skill("record-delivery", world.delivery("deal-1"), task_id="deal-1")
    world.flush()
    assert len(world.hook.requests) == 1
    world.service.ledger.append("deal-1", "trustee", "state", event="transition", state_to="DELIVERED")
    world.flush()
    assert len(world.hook.requests) == 1  # same state as already delivered


def test_4xx_from_the_receiver_is_not_retried(world: A2AWorld) -> None:
    world.client.call_skill("open-escrow", world.contract_input())
    hook = world.use_hook(Hook(statuses=[410]))
    world.client.set_push_config("deal-1", HOOK)
    world.client.call_skill("record-delivery", world.delivery(), task_id="deal-1")
    world.flush()
    assert len(hook.requests) == 1


def test_push_notification_via_sendmessage_is_refused_for_a_bad_url_before_anything_runs(world: A2AWorld) -> None:
    cfg = TaskPushNotificationConfig(url="http://evil.example.test/hook")
    out = rpc(
        world,
        "SendMessage",
        {
            "message": skill_message("open-escrow", world.contract_input()),
            "configuration": {"taskPushNotificationConfig": cfg.dump()},
        },
    )
    assert out["error"]["code"] == -32602
    assert world.paypal.total == 0  # the escrow was not opened


# ------------------------------------------------------------------------------------ durability


def test_restart_with_a_file_store_keeps_the_task_and_its_webhook(tmp_path: Path) -> None:
    db = tmp_path / "trustee.db"
    w1 = A2AWorld(db).setup()
    w1.client.call_skill("open-escrow", w1.contract_input())
    w1.client.set_push_config("deal-1", HOOK, token=HOOK_TOKEN, config_id="keep")
    w1.client.call_skill("record-delivery", w1.delivery(), task_id="deal-1")
    before = w1.client.get_task("deal-1")
    assert before.status.state == TaskState.WORKING
    w1.http.close()
    w1.store.close()

    w2 = A2AWorld(db)
    w2.keys = w1.keys  # same parties, new process
    after = w2.client.get_task("deal-1")
    assert after.status.state == TaskState.WORKING and after.id == "deal-1"
    assert after.metadata["trustee"]["contractDigest"] == before.metadata["trustee"]["contractDigest"]  # type: ignore[index]
    assert [c.id for c in w2.client.list_push_configs("deal-1")] == ["keep"]

    done = as_task(w2.client.call_skill("attest-delivery", w2.attestation(), task_id="deal-1"))
    assert done.status.state == TaskState.COMPLETED
    w2.flush()
    assert [t.status.state for t in w2.hook.tasks()] == [TaskState.COMPLETED]  # the stored webhook still fires
    assert w2.hook.requests[0].headers["x-a2a-notification-token"] == HOOK_TOKEN
    ids = [t.id for t in w2.client.list_tasks()["tasks"]]
    assert ids == ["deal-1"]


def test_a_restarted_server_serves_a_task_that_finished_before_the_restart(tmp_path: Path) -> None:
    db = tmp_path / "trustee.db"
    w1 = A2AWorld(db).setup()
    w1.client.call_skill("open-escrow", w1.contract_input())
    w1.client.call_skill("record-delivery", w1.delivery(), task_id="deal-1")
    w1.client.call_skill("attest-delivery", w1.attestation(), task_id="deal-1")
    receipt = artifact_data(w1.client.get_task("deal-1"), "settlement-receipt")
    w1.http.close()
    w1.store.close()
    w2 = A2AWorld(db)
    again = w2.client.get_task("deal-1")
    assert again.status.state == TaskState.COMPLETED and artifact_data(again, "settlement-receipt") == receipt


def test_the_store_migration_is_additive(tmp_path: Path) -> None:
    from trustee.store import Store

    path = tmp_path / "old.db"
    with Store(path) as s:
        assert s.schema_version() == 1  # no version bump: older builds still open the file
        s.put_push_config("c", "t", "https://x.example.test/h", "tok", "Bearer", "cred", "v1")
        assert s.push_config_task_ids() == {"t"}
    with Store(path) as s:
        assert s.get_push_config("t", "c")["token"] == "tok"  # type: ignore[index]
        assert s.delete_push_config("t", "c") is True and s.delete_push_config("t", "c") is False
