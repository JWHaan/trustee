"""AI-drafted acceptance criteria: the model proposes, code keeps only what the request says, and Trustee executes the
same criteria on the delivered file."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_platform_api import T0
from test_verification import GOOD_FILE, JUNK_FILE, PARTIAL_FILE, DataWorld, sha

from trustee.criteria import CriteriaError, check_csv, checkable_csv, draft_criteria
from trustee.protocol import Contract, GoodSpec

REQUEST = (
    "Q3 2026 returns data: at least 5,000 orders with columns order_id, sku, category, order_date, return_date, "
    "reason_code and refund_usd; no blank order_id or refund_usd."
)
DEMO_CRITERIA = [
    "sha256 matches contract",
    "schema: order_id, sku, category, order_date, return_date, reason_code, refund_usd",
    "at least 5000 rows, no blank order_id or refund_usd",
]


class FakeLLM:
    def __init__(self, reply: Any = None, fail: bool = False):
        self.reply, self.fail = reply, fail

    def complete_json(self, system: str, messages: list[dict[str, str]]) -> Any:
        if self.fail:
            raise TimeoutError("model down")
        return self.reply


def test_rules_turn_the_request_into_the_demo_contracts_criteria():
    draft = draft_criteria(REQUEST, None, sha256="a" * 64)
    assert draft.texts() == DEMO_CRITERIA
    assert draft.source == "rules" and all(c.checkable for c in draft.criteria)


def test_the_model_proposes_but_only_what_the_request_says_is_kept():
    reply = {
        "columns": ["order_id", "sku", "customer_email"],  # customer_email is not in the request
        "min_rows": 50000,  # not in the request either
        "required": ["order_id"],
        "market_check": False,
        "other": ["data must be from Q3 2026"],
    }
    draft = draft_criteria("returns data with columns order_id and sku, at least 5,000 rows", FakeLLM(reply))
    assert draft.source == "llm"
    texts = draft.texts()
    assert "schema: order_id, sku" in texts
    assert "at least 5000 rows, no blank order_id" in texts  # the model's 50000 was dropped for the stated 5,000
    manual = [c for c in draft.criteria if c.kind == "manual"]
    assert manual and manual[0].checkable is False  # Trustee cannot check it: a person decides
    assert any("customer_email" in n for n in draft.notes) and any("50000" in n for n in draft.notes)


def test_a_model_failure_falls_back_to_the_rules():
    draft = draft_criteria(REQUEST, FakeLLM(fail=True))
    assert (
        draft.source == "rules"
        and "schema: order_id, sku, category, order_date, return_date, reason_code, refund_usd" in draft.texts()
    )


def test_price_feeds_get_the_market_check():
    draft = draft_criteria("a competitor price feed with columns retailer, product_url, price; at least 40 rows")
    assert any(c.kind == "market" for c in draft.criteria)


def test_nothing_checkable_is_an_error():
    with pytest.raises(CriteriaError):
        draft_criteria("   ")
    with pytest.raises(CriteriaError):
        draft_criteria("something nice, please")


def _contract(criteria: list[str], file: bytes, good_id: str = "custom-returns-feed") -> Contract:
    return Contract(
        deal_id="deal-1",
        buyer="buyer",
        seller="seller",
        good=GoodSpec(
            good_id=good_id,
            description="returns data",
            category="data",
            acceptance_criteria=criteria,
            deliverable_ref="returns-2026q3.csv",
            sha256=sha(file),
        ),
        price=Decimal("100.00"),
        deadline=T0.replace(year=T0.year + 1),
        formed_at=T0,
    )


@pytest.mark.parametrize(
    ("file", "ok", "why"),
    [
        (GOOD_FILE, True, "criteria met"),
        (JUNK_FILE, False, "schema mismatch"),
        (PARTIAL_FILE, False, "blank refund_usd"),
    ],
)
def test_trustee_executes_the_drafted_criteria_on_any_csv(tmp_path: Path, file: bytes, ok: bool, why: str):
    contract = _contract(DEMO_CRITERIA, file)
    assert checkable_csv(contract)
    path = tmp_path / "returns.csv"
    path.write_bytes(file)
    passed, detail = check_csv(path, contract.good.good_id, contract=contract)
    assert passed is ok and why in detail


def test_a_new_data_good_is_verified_end_to_end_from_drafted_criteria():
    """No hand-written checker exists for this good: Trustee checks the file against the drafted criteria."""
    w = DataWorld().setup()
    criteria = w.http.post("/v1/criteria/draft", json={"request": REQUEST, "sha256": sha(JUNK_FILE)}).json()
    texts = [c["text"] for c in criteria["criteria"]]
    contract = _contract(texts, JUNK_FILE).sign("buyer", w.keys["buyer"]).sign("seller", w.keys["seller"])
    assert w.open(contract).status_code == 201
    w.upload(JUNK_FILE)
    w.deliver_hash(sha(JUNK_FILE))
    body = w.attest_with("reject", {}).json()
    # The hash matches what was signed, but the columns do not: refunded on Trustee's own check of the criteria.
    assert body["verification"]["hash_ok"] is True and body["verification"]["passed"] is False
    assert body["state"] == "VOIDED" and body["ruling"]["clause"].startswith("schema:")


def test_the_draft_endpoint_rejects_an_empty_request():
    w = DataWorld().setup()
    assert w.http.post("/v1/criteria/draft", json={"request": ""}).status_code == 422


def test_the_draft_endpoint_is_rate_limited_per_client():
    """It can call the LLM, so a public deployment caps drafts per client."""
    w = DataWorld().setup()
    for _ in range(10):
        assert w.http.post("/v1/criteria/draft", json={"request": REQUEST}).status_code == 200
    assert w.http.post("/v1/criteria/draft", json={"request": REQUEST}).status_code == 429
