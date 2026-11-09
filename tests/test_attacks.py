"""The attack set: every trick Trustee is meant to stop, as one table with an executable check per row.

Each case returns what actually happened in one short line, and asserts the defence held. CI runs them as tests;
`python scripts/attack_report.py` runs the same functions and prints the README table, so the published numbers come
from the code, not from prose. Add a row when a new defence is added.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_durability import Crash, IdempotentPayPal
from test_platform_api import World
from test_verification import GOOD_FILE, JUNK_FILE, PARTIAL_FILE, DataWorld, sha

from trustee.arena.runner import DealRunner
from trustee.arena.scenario import get_scenario
from trustee.identity import AgentKey
from trustee.ledger import Ledger
from trustee.paypal import PayPalClient
from trustee.platform.wire import revoke_signing_bytes
from trustee.protocol import Attest


@dataclass(frozen=True)
class Case:
    key: str
    attack: str  # what someone tries
    defence: str  # what Trustee does about it
    run: Callable[[Path], str]  # returns what happened; raises AssertionError if the defence did not hold


def _arena(name: str) -> dict:
    return (
        DealRunner(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
        .run(get_scenario(name), mode="scripted")
        .snapshot()
    )


# ------------------------------------------------------------------------------------------------- the cases


def honest_deal(_: Path) -> str:
    w = DataWorld().setup()
    w.open(w.data_contract())
    w.upload(GOOD_FILE)
    w.deliver_hash(sha(GOOD_FILE))
    body = w.attest_with("accept", {"sha256_check": "match"}).json()
    assert body["state"] == "SETTLED" and w.paypal.calls["capture"] == 1
    return "paid once"


def impostor_seller(_: Path) -> str:
    w = World().setup()
    forged = w.contract(sign=False).sign("buyer", w.keys["buyer"]).sign("seller", AgentKey.generate())
    r = w.open(forged)
    assert r.status_code == 401 and w.paypal.total == 0
    return "refused (401), PayPal never called"


def forged_attestation(_: Path) -> str:
    w = World().setup()
    w.open(w.contract())
    w.deliver("deal-1")
    fake = Attest(sender="buyer", deal_id="deal-1", verdict="accept", reason="ok").sign(w.keys["seller"])
    r = w.post("/v1/deals/deal-1/attest", fake.model_dump(mode="json"))
    assert r.status_code == 401 and w.paypal.calls["capture"] == 0
    return "refused (401), nothing captured"


def over_budget(_: Path) -> str:
    w = World().setup(buyer_cap="100.00")
    r = w.open(w.contract(price="150.00"))
    assert r.status_code == 403 and w.paypal.total == 0
    return "refused (403), PayPal never called"


def over_instruction(_: Path) -> str:
    snap = _arena("overreach")
    assert any("A person approved it" in r for r in snap["summary"]["reasons"])
    return "stepped up to a person"


def revoked_before_capture(_: Path) -> str:
    w = World().setup()
    w.open(w.contract())
    w.deliver("deal-1")
    sig = w.keys["org-b"].sign(revoke_signing_bytes("buyer"))
    assert w.post("/v1/mandates/buyer/revoke", {"signature": sig}).status_code == 200
    body = w.attest("deal-1", evidence={"sha256_check": "match"}).json()
    assert body["state"] == "DISPUTED" and w.paypal.calls["capture"] == 0
    return "held for a person, nothing captured"


def replayed_old_mandate(_: Path) -> str:
    from decimal import Decimal

    from trustee.mandate import Mandate

    w = World().setup()
    old = Mandate(agent_id="buyer", principal="org-b", max_amount=Decimal("9999"), grants="spend")
    r = w.mandate(old, w.keys["org-b"])
    assert r.status_code == 409
    return "refused (409)"


def junk_file(_: Path) -> str:
    w = DataWorld().setup()
    w.open(w.data_contract())
    w.upload(JUNK_FILE)
    w.deliver_hash(sha(JUNK_FILE))
    body = w.attest_with("reject", {}).json()
    assert body["state"] == "VOIDED" and w.paypal.calls["void"] == 1 and w.paypal.calls["capture"] == 0
    return "refunded on Trustee's own check"


def lying_seller_hash(_: Path) -> str:
    w = DataWorld().setup()
    w.open(w.data_contract())
    w.upload(JUNK_FILE)
    w.deliver_hash(sha(GOOD_FILE))
    body = w.attest_with("reject", {}).json()
    assert body["state"] == "VOIDED" and w.paypal.calls["capture"] == 0
    return "refunded, lie caught"


def buyer_frames_seller(_: Path) -> str:
    w = DataWorld().setup()
    w.open(w.data_contract())
    r = w.upload(JUNK_FILE, signer="buyer")  # the buyer hands Trustee junk for the deal
    w.deliver_hash(sha(GOOD_FILE))  # the seller sent the right file directly and signs its hash
    body = w.attest_with("reject", {}).json()
    assert r.status_code == 401 and w.service.blobs.seller_uploads("deal-1") == set()
    assert body["state"] != "VOIDED" and w.paypal.calls["void"] == 0
    return "upload refused (401), seller not framed"


def false_complaint(_: Path) -> str:
    w = DataWorld().setup()
    w.open(w.data_contract())
    w.upload(GOOD_FILE)
    w.deliver_hash(sha(GOOD_FILE))
    body = w.attest_with("reject", {"checker": "failed: schema mismatch"}).json()
    assert body["state"] == "DISPUTED" and w.paypal.calls["void"] == 0
    return "held for a person, no free refund"


def under_reported_rows(_: Path) -> str:
    w = DataWorld().setup()
    w.open(w.data_contract(PARTIAL_FILE, partial=True))
    w.upload(PARTIAL_FILE)
    w.deliver_hash(sha(PARTIAL_FILE))
    body = w.attest_with("reject", {"units_valid": "2600", "units_total": "5000"}).json()
    assert body["settlement"]["captured"] == "90.00"
    return "paid 90% (Trustee's count), not 52%"


def stale_price_feed(_: Path) -> str:
    snap = _arena("pricefeed-stale")
    assert snap["state"] == "VOIDED" and snap["verification"]["passed"] is False
    return "refunded on the market check"


def double_attest(_: Path) -> str:
    w = World().setup()
    w.open(w.contract())
    w.deliver("deal-1")
    w.attest("deal-1", evidence={"sha256_check": "match"})
    w.attest("deal-1", evidence={"sha256_check": "match"})
    assert w.paypal.calls["capture"] == 1
    return "captured once"


def duplicate_open(_: Path) -> str:
    w = World().setup()
    w.open(w.contract())
    w.open(w.contract())
    assert w.paypal.calls["authorize"] == 1
    return "held once"


def crash_after_paypal(tmp: Path) -> str:
    pp = IdempotentPayPal()
    w1 = World(tmp / "crash.db", paypal=pp).setup()
    pp.crash_after_authorize = True
    with pytest.raises(Crash):
        w1.service.open_deal(w1.contract())
    w1.store.close()
    pp.crash_after_authorize = False
    w2 = World(tmp / "crash.db", paypal=pp)
    w2.service.sweep()
    assert w2.http.get("/v1/deals/deal-1").json()["state"] == "ESCROWED" and len(pp.orders) == 1
    return "finished on restart, one hold"


def storage_down(_: Path) -> str:
    w = World().setup()

    def broken(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    w.store.upsert_deal = broken  # type: ignore[method-assign]
    assert w.open(w.contract()).status_code == 503 and w.paypal.total == 0
    return "refused (503), PayPal never called"


CASES: list[Case] = [
    Case("honest", "Honest deal (control)", "Pays the seller once", honest_deal),
    Case(
        "impostor",
        "Someone signs the contract as the seller with an unregistered key",
        "Refused before PayPal",
        impostor_seller,
    ),
    Case(
        "forged-attest",
        'The seller signs an "accept" as if it were the buyer',
        "Refused; nothing captured",
        forged_attestation,
    ),
    Case(
        "over-budget", "The buyer's agent agrees a price above its spending limit", "Refused before PayPal", over_budget
    ),
    Case(
        "over-instruction",
        "The agent pays more than the human's signed instruction allows",
        "Steps up to a person",
        over_instruction,
    ),
    Case(
        "revoked",
        "The human revokes the agent's limit after funds are held",
        "Held for a person, no capture",
        revoked_before_capture,
    ),
    Case("mandate-replay", "Someone replays an older, looser signed limit", "Refused", replayed_old_mandate),
    Case("junk-file", "The seller ships a junk file", "Refunded on Trustee's own check", junk_file),
    Case(
        "lying-hash",
        "The seller signs the contract's hash but hands over a different file",
        "Refunded; the lie is caught",
        lying_seller_hash,
    ),
    Case(
        "framed-seller",
        "The buyer uploads a junk file to frame a seller who delivered directly",
        "Ignored: only the seller's signed upload counts",
        buyer_frames_seller,
    ),
    Case(
        "false-complaint",
        "The buyer gets a correct file and claims it failed",
        "Held for a person; no free refund",
        false_complaint,
    ),
    Case(
        "under-report",
        "The buyer under-reports valid rows to pay less",
        "Pays by Trustee's own count",
        under_reported_rows,
    ),
    Case(
        "stale-feed",
        "The seller sells a price feed with stale prices",
        "Refunded on the market check",
        stale_price_feed,
    ),
    Case("double-attest", "The buyer's accept arrives twice", "Captured once", double_attest),
    Case("duplicate-open", "The same deal is opened twice", "Held once", duplicate_open),
    Case(
        "crash",
        "The server dies right after PayPal holds the money",
        "Finished on restart; one hold",
        crash_after_paypal,
    ),
    Case("storage-down", "The database fails just before a payment", "No PayPal call", storage_down),
]


@pytest.mark.parametrize("case", CASES, ids=[c.key for c in CASES])
def test_attack(case: Case, tmp_path: Path):
    case.run(tmp_path)
