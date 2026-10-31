"""Mandates: principal signatures, revocation, cumulative budgets."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from trustee.identity import AgentKey, PrincipalRegistry
from trustee.mandate import Mandate, MandateEngine, demo_mandates
from trustee.protocol import Accept, Contract, GoodSpec

GOOD = GoodSpec(good_id="g", description="d", category="code", acceptance_criteria=["c"], deliverable_ref="x.zip")


def contract(deal_id: str = "d1", price: str = "100.00") -> Contract:
    accept = Accept(
        sender="buyer-agent-1",
        responds_to="m",
        good=GOOD,
        price=Decimal(price),
        deadline=datetime.now() + timedelta(hours=1),
        deal_id=deal_id,
    )
    return Contract.from_accept(accept, seller="seller-agent-1")


def signed_engine(key: AgentKey | None = None, **buyer_overrides) -> tuple[MandateEngine, AgentKey]:
    key = key or AgentKey.generate()
    principals = PrincipalRegistry()
    principals.register("demo-principal", key.public_key_b64)
    ms = demo_mandates()
    if buyer_overrides:
        b = ms["buyer-agent-1"]
        ms["buyer-agent-1"] = Mandate(**{**b.__dict__, **buyer_overrides})
    return MandateEngine({a: m.sign(key) for a, m in ms.items()}, principals=principals), key


def test_unsigned_mandate_rejected_when_principals_set():
    principals = PrincipalRegistry()
    principals.register("demo-principal", AgentKey.generate().public_key_b64)
    engine = MandateEngine(demo_mandates(), principals=principals)
    report = engine.check_deal(contract())
    assert not report.ok
    assert sum("valid signature" in v for v in report.violations) == 2


def test_unsigned_mandate_fine_without_principals():
    assert MandateEngine(demo_mandates()).check_deal(contract()).ok


def test_signed_mandate_accepted():
    engine, _ = signed_engine()
    assert engine.check_deal(contract()).ok


def test_forged_principal_signature_rejected():
    engine, _ = signed_engine()
    forged = {a: m.sign(AgentKey.generate()) for a, m in demo_mandates().items()}
    engine = MandateEngine(forged, principals=engine.principals)
    assert not engine.check_deal(contract()).ok


def test_tampering_with_a_signed_mandate_invalidates_it():
    engine, _ = signed_engine()
    m = engine.get("buyer-agent-1")
    assert m is not None
    # signature kept, cap raised; version bumped because a same-version re-grant is now refused as a replay
    engine.grant(Mandate(**{**m.__dict__, "max_amount": Decimal("1000000"), "version": m.version + 1}))
    report = engine.check_deal(contract())
    assert not report.ok and any("signature" in v for v in report.violations)


def test_mandate_sign_does_not_mutate_and_is_deterministic_bytes():
    m = demo_mandates()["buyer-agent-1"]
    signed = m.sign(AgentKey.generate())
    assert m.principal_signature is None and signed.principal_signature
    assert m.signing_bytes() == signed.signing_bytes()


def test_expiry_semantics_unchanged():
    ms = demo_mandates()
    b = ms["buyer-agent-1"]
    ms["buyer-agent-1"] = Mandate(**{**b.__dict__, "valid_until": datetime(2020, 1, 1)})
    report = MandateEngine(ms).check_deal(contract())
    assert not report.ok and any("expired" in v for v in report.violations)
    assert MandateEngine(ms).check_deal(contract(), now=datetime(2019, 1, 1)).ok


def test_revoked_mandate_fails():
    engine = MandateEngine(demo_mandates())
    engine.revoke("seller-agent-1")
    report = engine.check_deal(contract())
    assert not report.ok and any("revoked" in v for v in report.violations)


def test_revoked_flag_on_mandate_fails():
    ms = demo_mandates()
    ms["buyer-agent-1"] = Mandate(**{**ms["buyer-agent-1"].__dict__, "revoked": True})
    assert not MandateEngine(ms).check_deal(contract()).ok


def test_budget_across_two_deals():
    engine, _ = signed_engine(max_total=Decimal("250.00"))
    c1, c2, c3 = contract("d1", "100.00"), contract("d2", "100.00"), contract("d3", "100.00")
    assert engine.check_deal(c1).ok
    engine.reserve(c1)
    assert engine.check_deal(c2).ok
    engine.reserve(c2)
    report = engine.check_deal(c3)
    assert not report.ok and any("budget" in v for v in report.violations)
    engine.release(c2)  # refunded: budget comes back
    assert engine.check_deal(c3).ok
    engine.commit(c1)
    engine.reserve(c3)
    assert engine.committed("buyer-agent-1") == Decimal("200.00")


def test_reserve_release_commit_are_idempotent():
    engine = MandateEngine(demo_mandates())
    c = contract()
    engine.reserve(c)
    engine.reserve(c)
    assert engine.committed("buyer-agent-1") == Decimal("100.00")
    engine.commit(c)
    engine.commit(c)
    engine.reserve(c)  # already spent: not reserved again
    engine.release(c)  # captured funds are never handed back
    assert engine.committed("buyer-agent-1") == Decimal("100.00")

    c2 = contract("d2")
    engine.reserve(c2)
    engine.release(c2)
    engine.release(c2)
    assert engine.committed("buyer-agent-1") == Decimal("100.00")


def test_own_reservation_does_not_count_against_itself():
    engine, _ = signed_engine(max_total=Decimal("100.00"))
    c = contract()
    engine.reserve(c)
    assert engine.check_deal(c).ok
    assert engine.recheck(c).ok


def test_recheck_fails_after_revoke_expiry_or_spent_budget():
    engine, _ = signed_engine(max_total=Decimal("150.00"))
    c = contract()
    engine.reserve(c)
    assert engine.recheck(c).ok
    other = contract("d2", "60.00")
    engine.reserve(other)  # budget consumed elsewhere after escrow
    assert not engine.recheck(c).ok
    engine.release(other)
    assert engine.recheck(c).ok
    engine.revoke("buyer-agent-1")
    assert not engine.recheck(c).ok
    fresh = MandateEngine(demo_mandates())
    ms = demo_mandates()
    ms["seller-agent-1"] = Mandate(
        **{**ms["seller-agent-1"].__dict__, "valid_until": datetime.now() + timedelta(hours=1)}
    )
    fresh = MandateEngine(ms)
    assert fresh.recheck(c).ok
    assert not fresh.recheck(c, now=datetime.now() + timedelta(hours=2)).ok
