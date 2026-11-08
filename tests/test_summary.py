"""The plain-language deal summary: one status word, one headline, a few reasons, the next step, and where the
money is. Agents read it from the API, people from the arena."""

from __future__ import annotations

from test_platform_api import GOOD_HASH, World

from trustee.arena.runner import DealRunner
from trustee.arena.scenario import get_scenario
from trustee.ledger import Ledger
from trustee.paypal import PayPalClient
from trustee.summary import summarize


def test_paid():
    s = summarize(state="SETTLED", price="156", buyer_reason="all acceptance criteria met", human_present=False)
    assert s["status"] == "paid" and s["tone"] == "good"
    assert s["headline"] == "Paid: the seller received 156.00 USD."
    assert s["reasons"][0] == "All acceptance criteria met"
    assert s["next_step"] is None
    assert s["money"] == {"paid_to_seller": "156.00", "returned_to_buyer": "0.00", "held": "0.00"}


def test_refunded_names_the_failed_clause_and_drops_internal_labels():
    ruling = {"decision": "release", "clause": "sha256 matches contract", "reasoning": "objective breach: hash differs"}
    s = summarize(state="VOIDED", price="170", escrow_status="voided", ruling=ruling)
    assert s["status"] == "refunded" and s["headline"] == "Refunded: the buyer keeps 170.00 USD."
    assert s["reasons"] == ["It failed the contract check “sha256 matches contract”.", "Hash differs"]
    assert s["money"]["returned_to_buyer"] == "170.00"


def test_partly_paid():
    settlement = {"captured": "140.40", "released": "15.60", "units_valid": 4500, "units_total": 5000}
    s = summarize(state="SETTLED", price="156", settlement=settlement)
    assert s["status"] == "partly_paid" and s["tone"] == "warn"
    assert "140.40" in s["headline"] and "15.60" in s["headline"]
    assert s["money"] == {"paid_to_seller": "140.40", "returned_to_buyer": "15.60", "held": "0.00"}


def test_refused_before_payment_moves_nothing():
    s = summarize(state="NEGOTIATING", price="156", outcome="rejected_identity", refusal="signature does not verify")
    assert s["status"] == "refused" and "No money moved" in s["headline"]
    assert s["reasons"] == ["Signature does not verify", "PayPal was never called."]
    assert set(s["money"].values()) == {"0.00"}


def test_cancelled_approval_and_void_without_hold():
    assert summarize(state="VOIDED", outcome="approval_timeout")["status"] == "cancelled"
    assert summarize(state="VOIDED", price="10", escrow_status="created")["status"] == "cancelled"


def test_on_hold_says_who_acts_next():
    s = summarize(
        state="DISPUTED",
        price="25",
        needs_attention="claim contradicts the signed record",
        dispute_deadline="2026-11-12T10:00:00",
    )
    assert s["status"] == "on_hold" and s["money"]["held"] == "25.00"
    assert "2026-11-12 10:00" in s["next_step"] and "operator" in s["next_step"]


def test_in_progress_states():
    assert summarize(state="ESCROWED", price="5")["headline"].startswith("Funds held")
    assert summarize(state="CONTRACTED", escrow_status="created")["status"] == "awaiting_approval"


def test_long_reasons_are_clipped_at_a_word():
    s = summarize(state="VOIDED", price="1", escrow_status="voided", refusal="word " * 80)
    assert len(s["reasons"][0]) <= 161 and s["reasons"][0].endswith("…")


def test_the_deal_api_carries_the_summary_for_agents():
    w = World().setup()
    w.open(w.contract())
    assert w.http.get("/v1/deals/deal-1").json()["summary"]["status"] == "in_progress"
    w.deliver("deal-1", content_hash=GOOD_HASH)
    w.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"})
    s = w.http.get("/v1/deals/deal-1").json()["summary"]
    assert s["status"] == "on_hold" and "contradicts the signed record" in s["reasons"][0]


def test_every_arena_scenario_ends_with_a_matching_summary():
    expected = {
        "honest": "paid",
        "cheat": "refunded",
        "partial": "partly_paid",
        "forged": "refused",
        "overreach": "paid",
        "pricefeed-stale": "refunded",
        "pricefeed-mixed": "partly_paid",
    }
    for name, status in expected.items():
        runner = DealRunner(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
        snap = runner.run(get_scenario(name), mode="scripted").snapshot()
        assert snap["summary"]["status"] == status, (name, snap["summary"])
        assert snap["summary"]["reasons"], name


def test_a2a_task_status_carries_the_summary():
    from trustee.a2a.tasks import deal_task

    w = World().setup()
    w.open(w.contract())
    task = deal_task(w.service.record("deal-1"))
    data = next(p for p in task.status.message.parts if getattr(p, "data", None) is not None).data
    assert data["summary"]["status"] == "in_progress"
    assert data["summary"]["headline"].startswith("Funds held")
