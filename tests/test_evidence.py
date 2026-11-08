"""The signed dispute evidence pack: everything needed to decide a dispute, checkable offline."""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient
from test_verification import GOOD_FILE, DataWorld, sha

from trustee.arena.api import create_app
from trustee.evidence import verify_evidence_pack
from trustee.ledger import Ledger
from trustee.paypal import PayPalClient


def disputed_world() -> DataWorld:
    """A correct file, verified by Trustee, that the buyer rejects anyway: the deal is held for a person."""
    w = DataWorld().setup()
    w.open(w.data_contract())
    w.upload(GOOD_FILE)
    w.deliver_hash(sha(GOOD_FILE))
    w.attest_with("reject", {"checker": "failed: schema mismatch"})
    return w


def test_the_pack_has_both_sides_and_trustees_own_check():
    w = disputed_world()
    pack = w.http.get("/v1/deals/deal-1/evidence").json()
    assert pack["type"] == "trustee.evidence-pack" and pack["state"] == "DISPUTED"
    assert set(pack["contract"]["signed"]["signatures"]) == {"buyer", "seller"}
    assert pack["paypal"]["custom_id"] == f"trustee:{pack['contract']['digest']}"
    assert pack["delivery"]["content_hash"] == sha(GOOD_FILE)  # what the seller signed
    assert pack["trustee_check"]["passed"] is True  # what Trustee found itself
    assert pack["attestation"]["verdict"] == "reject"  # what the buyer said
    assert pack["ruling"]["decision"] == "uphold"
    assert pack["summary"]["status"] == "on_hold"
    assert pack["ledger"]["chain_ok"] is True and pack["ledger"]["count"] > 10
    assert verify_evidence_pack(pack) == (True, "ok")


def test_an_edited_pack_fails_its_check():
    pack = disputed_world().http.get("/v1/deals/deal-1/evidence").json()

    edited = copy.deepcopy(pack)
    edited["summary"]["headline"] = "Refunded: the buyer keeps $100.00."
    assert verify_evidence_pack(edited)[0] is False

    rewritten = copy.deepcopy(pack)
    rewritten["ledger"]["events"][3]["payload"]["note"] = "rewritten"
    assert verify_evidence_pack(rewritten)[0] is False  # the signature fails before the chain is even checked


def test_a_pack_is_checked_against_the_key_you_trust():
    w = disputed_world()
    pack = w.http.get("/v1/deals/deal-1/evidence").json()
    assert verify_evidence_pack(pack, public_key=w.service.signing_key.public_key_b64)[0] is True
    other = pack | {"trustee_key": "AAAA"}  # someone swaps in their own key name
    assert verify_evidence_pack(other, public_key=w.service.signing_key.public_key_b64)[0] is False


def test_unknown_deal_is_404():
    assert DataWorld().setup().http.get("/v1/deals/nope/evidence").status_code == 404


def test_the_arena_serves_a_pack_for_an_in_process_run():
    app = create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True))
    http = TestClient(app)
    run_id = http.post("/api/runs", json={"scenario": "cheat", "mode": "scripted"}).json()["run_id"]
    http.get(f"/api/runs/{run_id}/events")  # the stream ends when the run is done
    pack = http.get(f"/api/runs/{run_id}/evidence").json()
    assert pack["state"] == "VOIDED" and pack["summary"]["status"] == "refunded"
    assert pack["trustee_check"]["passed"] is False
    assert pack["receipt"]["outcome"] == "voided"
    assert verify_evidence_pack(pack) == (True, "ok")
