"""The platform made visible in the arena: identity rows, a forged signature, outside agents over /v1,
and the real-sandbox payer approval wait. All PayPal HTTP goes through `httpx.MockTransport` or dry-run."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_approval import FakePayPal

from trustee.arena.api import ApprovalRegistry, create_app
from trustee.arena.approval import ApprovalWait
from trustee.arena.rows import COLUMNS, to_row
from trustee.arena.runner import DealRunner
from trustee.arena.scenario import get_scenario
from trustee.ledger import Ledger
from trustee.paypal import PayPalClient


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


class Clock:
    """A fake clock whose sleep advances it, with an optional hook per tick."""

    def __init__(self, on_tick=None) -> None:
        self.now = 0.0
        self.ticks = 0
        self.on_tick = on_tick

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.ticks += 1
        if self.on_tick is not None:
            self.on_tick(self.ticks)

    def wait(self) -> ApprovalWait:
        return ApprovalWait(poll_seconds=3, timeout_seconds=30, sleep=self.sleep, clock=self)


def approve_on_tick(fake: FakePayPal, n: int):
    return lambda tick: setattr(fake, "order_status", "APPROVED") if tick == n else None


class ExplodingPayPal(PayPalClient):
    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.calls: list[str] = []

    def create_and_authorize(self, **kwargs):  # type: ignore[override]
        self.calls.append("authorize")
        raise AssertionError("an identity failure must never reach PayPal")

    def capture(self, order, *, request_id=None):  # type: ignore[override]
        self.calls.append("capture")
        raise AssertionError("capture without a held escrow")

    def void(self, order, reason="", *, request_id=None):  # type: ignore[override]
        self.calls.append("void")
        raise AssertionError("void without a held escrow")


class CountingPayPal(PayPalClient):
    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.ops: list[str] = []

    def create_and_authorize(self, **kwargs):  # type: ignore[override]
        self.ops.append("authorize")
        return super().create_and_authorize(**kwargs)

    def capture(self, order, *, request_id=None):  # type: ignore[override]
        self.ops.append("capture")
        return super().capture(order, request_id=request_id)


def tape(runner: DealRunner, deal_id: str) -> list[dict]:
    return [to_row(e, i) for i, e in enumerate(runner.ledger.tail(deal_id))]


def make_runner(paypal: PayPalClient, wait: ApprovalWait | None = None, approvals=None) -> DealRunner:
    return DealRunner(ledger=Ledger(), paypal=paypal, approval_wait=wait, approvals=approvals)


def make_app(paypal: PayPalClient):
    holder: dict = {}
    app = create_app(ledger=Ledger(), paypal=paypal, self_client=lambda: holder["http"])
    holder["http"] = TestClient(app)
    return app, holder["http"]


# --------------------------------------------------------------- identity rows


def test_identity_rows_carry_signer_and_check():
    runner = make_runner(PayPalClient(dry_run=True))
    result = runner.run(get_scenario("honest"), mode="scripted")
    rows = tape(runner, result.deal_id)
    ids = [r for r in rows if r["kind"] == "identity"]
    assert [r["check"] for r in ids] == ["verified"] * 4
    assert [r["signer"] for r in ids] == ["buyer-agent-1", "seller-agent-1", "seller-agent-1", "buyer-agent-1"]
    others = [r for r in rows if r["kind"] != "identity"]
    assert all(r["signer"] == "" and r["check"] == "" for r in others)


def test_row_columns_are_additive_and_stable():
    runner = make_runner(PayPalClient(dry_run=True))
    result = runner.run(get_scenario("honest"), mode="scripted")
    for row in tape(runner, result.deal_id):
        assert tuple(row) == COLUMNS
    assert COLUMNS[:15] == (
        "id", "seq", "ts", "deal", "actor", "kind", "event", "state_from", "state_to",
        "price", "verdict", "paypal_op", "order_id", "sha", "note",
    )  # fmt: skip


# ---------------------------------------------------------------------- forged


def test_forged_signature_is_rejected_before_paypal():
    paypal = ExplodingPayPal()
    runner = make_runner(paypal)
    result = runner.run(get_scenario("forged"), mode="scripted")

    assert paypal.calls == []
    assert result.outcome == "rejected_identity"
    assert result.error is None
    assert result.state == "NEGOTIATING"  # never contracted
    rows = tape(runner, result.deal_id)
    assert not [r for r in rows if r["kind"] == "paypal"]
    checks = [(r["signer"], r["check"]) for r in rows if r["kind"] == "identity"]
    assert checks == [("buyer-agent-1", "verified"), ("seller-agent-1", "rejected")]


def test_honest_and_cheat_outcomes_are_unchanged():
    runner = make_runner(PayPalClient(dry_run=True))
    honest = runner.run(get_scenario("honest"), mode="scripted")
    cheat = runner.run(get_scenario("cheat"), mode="scripted")
    assert (honest.state, honest.outcome) == ("SETTLED", None)
    assert (cheat.state, cheat.outcome) == ("VOIDED", None)


# --------------------------------------------------------------------- outside


def test_outside_agents_settle_through_the_v1_api():
    paypal = CountingPayPal()
    app, _ = make_app(paypal)
    runner: DealRunner = app.state.runner
    result = runner.run(get_scenario("outside"), mode="scripted")

    assert result.state == "SETTLED", result.error
    assert result.price == "156.00"
    assert paypal.ops.count("capture") == 1
    rows = tape(runner, result.deal_id)
    kinds = {r["kind"] for r in rows}
    assert {"api", "identity", "state", "paypal"} <= kinds
    api = [(r["actor"], r["event"], r["note"].split(" ")[0]) for r in rows if r["kind"] == "api"]
    assert any(a[1] == "POST /v1/deals" and a[2] == "201" for a in api)
    assert any(a[1] == "POST /v1/deals/{id}/deliverable" and a[2] == "201" for a in api)  # the seller's signed upload
    assert any(a[1] == "POST /v1/deals/{id}/delivery" and a[0].startswith("tidewater-sales") for a in api)
    assert any(a[1] == "POST /v1/deals/{id}/attest" and a[0].startswith("northwind-planner") for a in api)
    assert [r["check"] for r in rows if r["kind"] == "identity"] == ["verified"] * 4
    assert rows[-1]["event"] == "run_finished"


def test_outside_runs_can_repeat_on_one_service():
    app, _ = make_app(PayPalClient(dry_run=True))
    a = app.state.runner.run(get_scenario("outside"), mode="scripted")
    b = app.state.runner.run(get_scenario("outside"), mode="scripted")
    assert a.state == b.state == "SETTLED"
    assert a.deal_id != b.deal_id


def test_outside_waits_for_the_buyer_in_paypal():
    fake = FakePayPal()
    app, _ = make_app(fake.client())
    runner: DealRunner = app.state.runner
    runner.approval_wait = Clock(on_tick=approve_on_tick(fake, 2)).wait()

    result = runner.run(get_scenario("outside"), mode="scripted")

    assert result.state == "SETTLED", result.error
    assert len(fake.calls("/authorize")) == 1 and len(fake.calls("/capture")) == 1
    link = [r for r in tape(runner, result.deal_id) if r["event"] == "awaiting_approval"]
    assert link and link[0]["link"].endswith("token=ORD-1") and link[0]["wait_s"] == "30"


# ------------------------------------------------------------ payer approval


def test_run_pauses_with_a_link_row_then_settles_when_approved():
    fake = FakePayPal()
    clock = Clock(on_tick=approve_on_tick(fake, 2))
    runner = make_runner(fake.client(), clock.wait())
    result = runner.run(get_scenario("honest"), mode="scripted")

    assert result.state == "SETTLED", result.error
    assert len(fake.calls("/authorize")) == 1
    assert len(fake.calls("/capture")) == 1
    waiting = [r for r in tape(runner, result.deal_id) if r["event"] == "awaiting_approval"]
    assert len(waiting) == 1
    assert waiting[0]["link"] == "https://sandbox.paypal.test/approve?token=ORD-1"
    assert waiting[0]["order_id"] == "ORD-1"
    assert clock.ticks == 2
    assert result.escrow is not None and result.escrow["status"] == "captured"


def test_approval_timeout_voids_without_calling_paypal_money_ops():
    fake = FakePayPal()  # the order stays CREATED forever
    clock = Clock()
    runner = make_runner(fake.client(), clock.wait())
    result = runner.run(get_scenario("honest"), mode="scripted")

    assert result.state == "VOIDED"
    assert result.outcome == "approval_timeout"
    assert result.error is None
    assert fake.money_calls == 0
    assert clock.now >= 30


def test_cancelling_at_paypal_ends_the_wait():
    fake = FakePayPal()
    approvals = ApprovalRegistry()
    clock = Clock(on_tick=lambda n: approvals.cancel("ORD-1"))
    runner = make_runner(fake.client(), clock.wait(), approvals)
    result = runner.run(get_scenario("cheat"), mode="scripted")

    assert result.state == "VOIDED" and result.outcome == "approval_cancelled"
    assert fake.money_calls == 0


def test_forged_never_waits_for_approval():
    fake = FakePayPal()
    runner = make_runner(fake.client(), Clock().wait())
    result = runner.run(get_scenario("forged"), mode="scripted")
    assert result.outcome == "rejected_identity"
    assert fake.requests == []  # not even a token request


# ------------------------------------------------------------------- routes


def test_return_and_cancel_for_an_arena_order_redirect_to_the_arena():
    app, http = make_app(PayPalClient(dry_run=True))
    registry: ApprovalRegistry = app.state.approvals
    event = registry.track("ORD-ARENA")

    r = http.get("/v1/paypal/return", params={"token": "ORD-ARENA"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert not event.is_set()

    r = http.get("/v1/paypal/cancel", params={"token": "ORD-ARENA"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert event.is_set()


def test_unknown_tokens_still_404():
    _, http = make_app(PayPalClient(dry_run=True))
    assert http.get("/v1/paypal/return", params={"token": "NOPE"}).status_code == 404
    assert http.get("/v1/paypal/cancel", params={"token": "NOPE"}).status_code == 404


def test_registry_is_bounded():
    registry = ApprovalRegistry(cap=2)
    for oid in ("A", "B", "C"):
        registry.track(oid)
    assert not registry.owns("A") and registry.owns("B") and registry.owns("C")
    assert isinstance(registry.track("C"), threading.Event)
