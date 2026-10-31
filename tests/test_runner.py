"""Arena runner tests: both scenarios, the parity claim, and the safety claims.

The plan asks for "scripted replay parity: event sequence identical, minus
timestamps/ids". That test is only meaningful if you define what counts as an
id, so `_shape` below is the definition: everything that is either a clock
reading or a minted identifier is normalised away, and the remaining
(kind, actor, state_from, state_to, event) sequence must match exactly between
two runs of the same scenario.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from trustee.arena.rows import to_row
from trustee.arena.runner import DealRunner
from trustee.arena.scenario import get_scenario, load_scenarios
from trustee.ledger import Ledger
from trustee.mandate import Mandate, MandateEngine, demo_mandates
from trustee.paypal import PayPalClient

MINTED = re.compile(r"(dry-|deal_|run_|msg_)[0-9a-f]{6,}")


def make_runner(paypal: PayPalClient | None = None) -> tuple[DealRunner, Ledger]:
    ledger = Ledger()
    return DealRunner(ledger=ledger, paypal=paypal or PayPalClient(dry_run=True)), ledger


def _shape(rows: list[dict]) -> list[tuple]:
    return [(r["actor"], r["kind"], r["state_from"], r["state_to"], r["event"]) for r in rows]


# ------------------------------------------------------------------ scenarios


def test_honest_run_settles():
    runner, ledger = make_runner()
    result = runner.run(get_scenario("honest"), mode="scripted")

    assert result.state == "SETTLED", result.error
    assert result.effective_mode == "scripted"
    assert result.price == "156.00"
    assert result.escrow is not None and result.escrow["status"] == "captured"
    assert result.attest is not None and result.attest["verdict"] == "accept"
    assert result.ruling is None  # no dispute on the happy path
    assert ledger.tail(result.deal_id, kinds=("paypal",))[-1].payload["op"] == "capture"


def test_cheat_run_voids_and_refunds():
    runner, _ = make_runner()
    result = runner.run(get_scenario("cheat"), mode="scripted")

    assert result.state == "VOIDED", result.error
    assert result.ruling is not None
    assert result.ruling["decision"] == "release"
    assert result.ruling["clause"] == "sha256 in manifest"
    assert result.attest is not None and result.attest["verdict"] == "reject"
    # two independent criteria failed: hash and exported surface
    assert result.attest["evidence"]["sha256_check"] == "mismatch"
    assert "missing exports" in result.attest["evidence"]["checker"]
    assert result.escrow is not None and result.escrow["status"] == "voided"


def test_scenarios_differ_only_by_the_cheat_flag():
    honest, cheat = load_scenarios()["honest"], load_scenarios()["cheat"]
    assert honest.good == cheat.good
    assert honest.cheat is False and cheat.cheat is True


# -------------------------------------------------------------------- parity


def test_scripted_replay_is_deterministic():
    """Same scenario twice => identical event shape, identical prices."""
    a_runner, _ = make_runner()
    a = a_runner.run(get_scenario("honest"), mode="scripted")
    rows_a = [to_row(e, i) for i, e in enumerate(a_runner.ledger.tail(a.deal_id))]

    b_runner, _ = make_runner()
    b = b_runner.run(get_scenario("honest"), mode="scripted")
    rows_b = [to_row(e, i) for i, e in enumerate(b_runner.ledger.tail(b.deal_id))]

    assert _shape(rows_a) == _shape(rows_b)
    assert [r["price"] for r in rows_a] == [r["price"] for r in rows_b]


def test_ledger_rows_are_deal_scoped():
    """Every row of a run belongs to that run's deal — the grid groups on it."""
    runner, _ = make_runner()
    result = runner.run(get_scenario("honest"), mode="scripted")
    rows = [to_row(e, i) for i, e in enumerate(runner.ledger.tail(result.deal_id))]
    assert rows
    assert all(r["deal"] == result.deal_id for r in rows)


def test_state_rows_are_emitted_in_order():
    """The tape must read top-down: money op, then the state it caused."""
    runner, _ = make_runner()
    result = runner.run(get_scenario("cheat"), mode="scripted")
    events = runner.ledger.tail(result.deal_id)
    transitions = [e.payload["state_to"] for e in events if e.kind == "state"]
    assert transitions == ["NEGOTIATING", "CONTRACTED", "ESCROWED", "DELIVERED", "DISPUTED", "VOIDED"]

    kinds = [e.kind for e in events]
    disputed = next(i for i, e in enumerate(events) if e.kind == "state" and e.payload["state_to"] == "DISPUTED")
    ruling = kinds.index("ruling")
    void = next(i for i, e in enumerate(events) if e.kind == "paypal" and e.payload.get("op") == "void")
    assert disputed < ruling < void


# --------------------------------------------------------------------- safety


class ExplodingPayPal(PayPalClient):
    """Fails the test if the money path is touched at all."""

    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.calls: list[str] = []

    def create_and_authorize(self, **kwargs):  # type: ignore[override]
        self.calls.append("authorize")
        raise AssertionError("mandate gate must run before any PayPal call")

    def capture(self, order):  # type: ignore[override]
        self.calls.append("capture")
        raise AssertionError("capture without a held escrow")

    def void(self, order, reason=""):  # type: ignore[override]
        self.calls.append("void")
        raise AssertionError("void without a held escrow")


def test_mandate_refusal_never_reaches_paypal():
    """The headline claim: a forbidden deal cannot touch the money path."""
    paypal = ExplodingPayPal()
    ledger = Ledger()
    # Cap the buyer's mandate below the negotiated price.
    mandates = demo_mandates()
    buyer = mandates["buyer-agent-1"]
    mandates["buyer-agent-1"] = Mandate(
        agent_id=buyer.agent_id,
        principal=buyer.principal,
        max_amount=Decimal("100.00"),
        allowed_categories=buyer.allowed_categories,
        allowed_counterparties=buyer.allowed_counterparties,
        grants="spend",
    )
    runner = DealRunner(ledger=ledger, paypal=paypal, mandates=MandateEngine(mandates))

    result = runner.run(get_scenario("honest"), mode="scripted")

    assert paypal.calls == []
    assert result.state == "NEGOTIATING"  # never contracted
    assert any(e.kind == "mandate" and e.payload.get("ok") is False for e in ledger.tail(result.deal_id))


def test_exactly_one_capture_on_settlement():
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

    paypal = CountingPayPal()
    runner, _ = make_runner(paypal)
    runner.run(get_scenario("honest"), mode="scripted")
    assert paypal.ops == ["authorize", "capture"]


# ----------------------------------------------------------------------- modes


def test_live_mode_without_a_key_degrades_to_scripted(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    runner, _ = make_runner()
    result = runner.run(get_scenario("honest"), mode="auto")
    assert result.effective_mode == "scripted"
    assert result.mode == "auto"
    assert result.state == "SETTLED"


def test_live_failure_falls_back_and_says_so(monkeypatch):
    """`auto` must survive a broken LLM and label the tape honestly."""
    monkeypatch.setenv("LLM_API_KEY", "sk-not-a-real-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")  # nothing listens here
    runner, ledger = make_runner()
    result = runner.run(get_scenario("honest"), mode="auto")

    assert result.state == "SETTLED"
    assert result.effective_mode == "scripted"
    assert result.fallback_from == "live"
    started = [e for e in ledger.tail(result.deal_id) if e.payload.get("event") == "run_started"]
    assert started and "live failed" in started[-1].payload["note"]


def test_live_mode_fails_loudly_when_asked(monkeypatch):
    """`live` is the mode you run the reliability drill in: no silent rescue."""
    monkeypatch.setenv("LLM_API_KEY", "sk-not-a-real-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    runner, _ = make_runner()
    result = runner.run(get_scenario("honest"), mode="live")
    # `live` still falls back (reliability doctrine), but reports the failure.
    assert result.fallback_from == "live"
    assert result.effective_mode == "scripted"


def test_unknown_mode_is_rejected():
    runner, _ = make_runner()
    with pytest.raises(ValueError):
        runner.run(get_scenario("honest"), mode="yolo")


# -------------------------------------------------------------------- identity


def test_runs_verify_signatures_on_the_tape():
    runner, ledger = make_runner()
    result = runner.run(get_scenario("honest"), mode="scripted")
    ids = [e.payload for e in ledger.tail(result.deal_id, kinds=("identity",))]
    assert [i["event"] for i in ids] == ["verify_contract", "verify_contract", "verify_delivery", "verify_attest"]
    assert all(i["ok"] for i in ids)
    assert result.contract is not None and len(result.contract["digest"]) == 64


def test_cheat_run_also_verifies_signatures():
    runner, ledger = make_runner()
    result = runner.run(get_scenario("cheat"), mode="scripted")
    assert result.state == "VOIDED"
    assert len(ledger.tail(result.deal_id, kinds=("identity",))) == 4


def test_keys_are_fresh_per_run():
    runner, ledger = make_runner()
    a = runner.run(get_scenario("honest"), mode="scripted")
    b = runner.run(get_scenario("honest"), mode="scripted")
    assert a.state == b.state == "SETTLED"
    assert a.contract is not None and b.contract is not None
    assert a.contract["digest"] != b.contract["digest"]  # distinct deals, distinct signed terms
