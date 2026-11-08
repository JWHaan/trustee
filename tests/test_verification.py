"""Trustee checks the delivered file itself, and rules on its own result rather than on the buyer's report.

Each test here describes a case the previous design got wrong, because Trustee only saw the buyer's claims:
a buyer's false complaint refunded them, an unexplained rejection of a junk file left the money stuck, a buyer who
under-reported valid rows paid less, and the seller's file reached the buyer before the payment did."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from test_platform_api import T0, World

from trustee.arena.runner import DealRunner
from trustee.arena.scenario import FIXTURES, get_scenario
from trustee.identity import AgentKey
from trustee.ledger import Ledger
from trustee.mandate import Mandate
from trustee.paypal import PayPalClient
from trustee.platform.wire import download_signing_bytes
from trustee.protocol import Attest, Contract, Delivery, GoodSpec, PartialTerms
from trustee.verification import BlobError, BlobStore

GOODS = FIXTURES / "goods"
GOOD_FILE = (GOODS / "returns-2026q3.csv").read_bytes()
JUNK_FILE = (GOODS / "returns-2026q3-junk.csv").read_bytes()
PARTIAL_FILE = (GOODS / "returns-2026q3-partial.csv").read_bytes()
CRITERIA = [
    "sha256 matches contract",
    "schema: order_id, sku, category, order_date, return_date, reason_code, refund_usd",
    "at least 5000 rows, no blank order_id or refund_usd",
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DataWorld(World):
    """The platform world, with mandates that allow data goods and a contract for the returns dataset."""

    def setup(self, buyer_cap: str = "500.00", max_total: str | None = None) -> DataWorld:
        super().setup(buyer_cap, max_total)
        for agent, principal, grants, counterparty in (
            ("buyer", "org-b", "spend", "seller"),
            ("seller", "org-s", "receive", "buyer"),
        ):
            m = Mandate(
                agent_id=agent,
                principal=principal,
                max_amount=Decimal("500.00"),
                allowed_categories=frozenset({"code", "data"}),
                allowed_counterparties=frozenset({counterparty}),
                grants=grants,  # type: ignore[arg-type]
                version=2,
            )
            assert self.mandate(m, self.keys[principal]).status_code == 201
        return self

    def data_contract(self, file: bytes = GOOD_FILE, partial: bool = False, deal_id: str = "deal-1") -> Contract:
        c = Contract(
            deal_id=deal_id,
            buyer="buyer",
            seller="seller",
            good=GoodSpec(
                good_id="returns-dataset-2026q3",
                description="Q3 2026 returns dataset",
                category="data",
                acceptance_criteria=CRITERIA,
                deliverable_ref="returns-2026q3.csv",
                sha256=sha(file),
            ),
            price=Decimal("100.00"),
            deadline=T0.replace(year=T0.year + 1),
            formed_at=T0,
            partial=PartialTerms(min_fraction=Decimal("0.5")) if partial else None,
        )
        return c.sign("buyer", self.keys["buyer"]).sign("seller", self.keys["seller"])

    def upload(self, data: bytes, deal_id: str = "deal-1"):
        return self.http.post(
            f"/v1/deals/{deal_id}/deliverable", content=data, headers={"Content-Type": "application/octet-stream"}
        )

    def deliver_hash(self, content_hash: str, deal_id: str = "deal-1"):
        d = Delivery(sender="seller", deal_id=deal_id, deliverable_ref="returns-2026q3.csv", content_hash=content_hash)
        return self.post(f"/v1/deals/{deal_id}/delivery", d.sign(self.keys["seller"]).model_dump(mode="json"))

    def attest_with(self, verdict: str, evidence: dict[str, str], deal_id: str = "deal-1"):
        a = Attest(sender="buyer", deal_id=deal_id, verdict=verdict, reason="buyer says so", evidence=evidence)  # type: ignore[arg-type]
        return self.post(f"/v1/deals/{deal_id}/attest", a.sign(self.keys["buyer"]).model_dump(mode="json"))


@pytest.fixture
def w() -> DataWorld:
    return DataWorld().setup()


def test_a_false_complaint_about_a_file_trustee_verified_is_held_not_refunded(w: DataWorld):
    assert w.open(w.data_contract()).json()["state"] == "ESCROWED"
    assert w.upload(GOOD_FILE).status_code == 201
    assert w.deliver_hash(sha(GOOD_FILE)).json()["state"] == "DELIVERED"
    r = w.attest_with("reject", {"checker": "failed: schema mismatch"})
    body = r.json()
    assert body["state"] == "DISPUTED" and body["escrow"]["status"] == "authorized"
    assert w.paypal.calls["void"] == 0
    assert body["verification"]["passed"] is True
    assert "every contract check passed" in body["needs_attention"]


def test_a_junk_file_is_refunded_on_trustees_own_check(w: DataWorld):
    """The buyer rejects without evidence. Before, that was a dispute with the money held; Trustee's check decides."""
    w.open(w.data_contract())
    w.upload(JUNK_FILE)
    w.deliver_hash(sha(JUNK_FILE))
    body = w.attest_with("reject", {}).json()
    assert body["state"] == "VOIDED" and w.paypal.calls["void"] == 1
    assert body["verification"]["passed"] is False and body["verification"]["hash_ok"] is False
    assert body["ruling"]["clause"] == "sha256 matches contract" and body["ruling"]["objective"] is True
    assert body["summary"]["status"] == "refunded"


def test_a_seller_who_signs_a_hash_for_a_file_it_did_not_hand_over_is_caught(w: DataWorld):
    w.open(w.data_contract())
    w.upload(JUNK_FILE)
    w.deliver_hash(sha(GOOD_FILE))  # claims the contracted file, uploaded junk
    body = w.attest_with("reject", {}).json()
    assert body["state"] == "VOIDED"
    assert "did not hand over" in body["ruling"]["reasoning"]


def test_trustees_count_sets_a_partial_payment_not_the_buyers(w: DataWorld):
    """The partial file has 4,500 valid rows of 5,000. A buyer reporting 2,600 would have paid 52%; it pays 90%."""
    w.open(w.data_contract(PARTIAL_FILE, partial=True))
    w.upload(PARTIAL_FILE)
    w.deliver_hash(sha(PARTIAL_FILE))
    body = w.attest_with("reject", {"units_valid": "2600", "units_total": "5000"}).json()
    assert body["state"] == "SETTLED"
    assert body["settlement"]["units_valid"] == 4500 and body["settlement"]["captured"] == "90.00"


def test_the_buyer_gets_the_file_from_trustee_only_after_payment_settles(w: DataWorld):
    w.open(w.data_contract())
    w.upload(GOOD_FILE)
    w.deliver_hash(sha(GOOD_FILE))

    def download(key: AgentKey):
        sig = key.sign(download_signing_bytes("deal-1"))
        return w.http.get("/v1/deals/deal-1/deliverable", headers={"X-Trustee-Signature": sig})

    assert download(w.keys["buyer"]).status_code == 409  # not paid yet
    assert w.attest_with("accept", {"sha256_check": "match"}).json()["state"] == "SETTLED"
    assert download(w.keys["seller"]).status_code == 401  # only the buyer
    got = download(w.keys["buyer"])
    assert got.status_code == 200 and got.content == GOOD_FILE


def test_without_an_upload_the_buyers_check_still_works(w: DataWorld):
    w.open(w.data_contract())
    w.deliver_hash(sha(GOOD_FILE))
    body = w.attest_with("accept", {"sha256_check": "match"}).json()
    assert body["state"] == "SETTLED" and "verification" not in body
    notes = [e["payload"] for e in w.http.get("/v1/deals/deal-1/events").json()["events"] if e["kind"] == "note"]
    assert any("no file to check" in n.get("explanation", "") for n in notes)


def test_upload_rules(w: DataWorld):
    assert w.upload(GOOD_FILE, deal_id="nope").status_code == 404
    w.open(w.data_contract())
    assert w.upload(b"").status_code == 422
    assert w.upload(GOOD_FILE).json()["sha256"] == sha(GOOD_FILE)
    w.deliver_hash(sha(GOOD_FILE))
    w.attest_with("accept", {"sha256_check": "match"})
    assert w.upload(GOOD_FILE).status_code == 409  # settled: no more files


def test_blob_store_limits(tmp_path: Path):
    store = BlobStore(tmp_path, max_bytes=10)
    with pytest.raises(BlobError):
        store.put(b"x" * 11)
    digest = store.put(b"hello", "d1")
    assert store.get(digest) == b"hello" and store.uploaded_for("d1") == {digest}
    assert store.get("../../etc/passwd") is None


@pytest.mark.parametrize(
    ("scenario", "passed"),
    [("honest", True), ("cheat", False), ("partial", False), ("pricefeed-stale", False)],
)
def test_the_arena_shows_trustees_own_check(scenario: str, passed: bool):
    ledger = Ledger()
    result = DealRunner(ledger=ledger, paypal=PayPalClient(dry_run=True)).run(get_scenario(scenario), mode="scripted")
    rows = [e for e in ledger.tail(result.deal_id) if e.kind == "verification"]
    assert len(rows) == 1 and rows[0].payload["ok"] is passed
    assert result.snapshot()["verification"]["passed"] is passed


def test_outside_agents_hand_trustee_the_file_over_the_public_api():
    """The `outside` scenario's seller uploads through /v1; Trustee's own check lands on the run's snapshot."""
    from test_arena_platform import make_app

    app, _ = make_app(PayPalClient(dry_run=True))
    snap = app.state.runner.run(get_scenario("outside"), mode="scripted").snapshot()
    assert snap["state"] == "SETTLED"
    assert snap["verification"]["passed"] is True
    assert any("Trustee checked the file itself" in r for r in snap["summary"]["reasons"])
