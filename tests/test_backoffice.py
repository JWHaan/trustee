"""Zapier MCP back office: allowlist, money-tool refusal, argument building, idempotency, failure isolation.

No network: every test uses a fake MCP session, and the real `ZapierMcpSession` is never constructed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from trustee.network import backoffice as bo
from trustee.network.backoffice import (
    Backoffice,
    BackofficeConfigError,
    Facts,
    build_arguments,
    facts_from_receipt,
    is_money_tool,
    parse_tool_map,
)

TW_TOOLS = {"record_invoice": "quickbooks_create_invoice", "update_crm": "hubspot_update_deal"}
NW_TOOLS = {
    "record_bill": "quickbooks_create_bill",
    "post_finance_note": "slack_post_message",
    "notify_principal": "gmail_send_email",
}


@dataclass
class FakeSession:
    fail: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def list_tools(self) -> list[str]:
        return sorted(set(TW_TOOLS.values()) | set(NW_TOOLS.values()))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append((name, arguments))
        if self.fail:
            raise RuntimeError("zapier is down")


def receipt(outcome: str = "settled", amount: str = "42.00", capture: str | None = "CAP-1") -> dict[str, Any]:
    return {
        "type": "trustee.settlement_receipt",
        "deal_id": "deal_ab12",
        "outcome": outcome,
        "amount": amount,
        "currency": "USD",
        "buyer": "northwind-planner-x",
        "seller": "tidewater-sales-y",
        "delivery_sha256": "ab" * 32,
        "paypal": {"order_id": "O-1", "authorization_id": "A-1", "capture_id": capture, "voided": outcome == "voided"},
        "signature": "sig",
    }


def make(agent: str, session: FakeSession | None, tools: dict[str, str] | None = None, **kw: Any) -> Backoffice:
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    default = TW_TOOLS if agent == "tidewater" else NW_TOOLS
    return Backoffice(agent, session=session, tools=default if tools is None else tools, **kw)


def wait(bk: Backoffice) -> None:
    assert bk.flush(timeout=5), "bookings did not finish"


# ------------------------------------------------------------------------- money-tool refusal


@pytest.mark.parametrize(
    "name",
    [
        "stripe_refund_charge",
        "paypal_create_payout",
        "bank_transfer",
        "capture_payment",
        "payments_pay",
        "paypal_order",
    ],
)
def test_money_moving_tool_names_are_refused(name: str) -> None:
    assert is_money_tool(name)


@pytest.mark.parametrize("name", ["quickbooks_create_invoice", "display_note", "hubspot_update_deal", "payroll_list"])
def test_ordinary_tool_names_are_allowed(name: str) -> None:
    # "display" and "payroll" contain "pay"/"play" as substrings but are not money words.
    assert not is_money_tool(name)


def test_parse_drops_money_tool_and_logs_without_mapping_it(caplog: pytest.LogCaptureFixture) -> None:
    raw = '{"record_invoice": "stripe_refund_invoice", "update_crm": "hubspot_update_deal"}'
    out = parse_tool_map(raw, "tidewater")
    assert out == {"update_crm": "hubspot_update_deal"}


def test_constructor_refuses_money_tool_even_when_built_in_code() -> None:
    session = FakeSession()
    bk = make("tidewater", session, tools={"record_invoice": "paypal_capture_order"})
    assert not bk.enabled
    assert bk.book("record_invoice", _facts()) is False
    assert session.calls == []


# ------------------------------------------------------------------------------- allowlist


def test_parse_rejects_non_object_and_invalid_json() -> None:
    with pytest.raises(BackofficeConfigError):
        parse_tool_map("[1, 2]", "tidewater")
    with pytest.raises(BackofficeConfigError):
        parse_tool_map("{not json", "tidewater")


def test_parse_ignores_actions_that_belong_to_the_other_agent() -> None:
    out = parse_tool_map('{"record_bill": "quickbooks_create_bill", "update_crm": "hubspot"}', "tidewater")
    assert out == {"update_crm": "hubspot"}


def test_parse_rejects_bad_tool_names() -> None:
    assert parse_tool_map('{"update_crm": "hubspot; rm -rf /"}', "tidewater") == {}


def test_unmapped_action_is_never_called() -> None:
    session = FakeSession()
    bk = make("tidewater", session, tools={"update_crm": "hubspot_update_deal"})
    assert bk.book("record_invoice", _facts()) is False
    assert bk.book("update_crm", _facts()) is True
    wait(bk)
    assert [name for name, _ in session.calls] == ["hubspot_update_deal"]


def test_an_agent_cannot_book_the_other_agents_actions() -> None:
    session = FakeSession()
    bk = make("tidewater", session)
    assert bk.book("record_bill", _facts()) is False
    assert bk.book("notify_principal", _facts(outcome="step_up")) is False


# ------------------------------------------------------------------------------- off by default


def test_no_env_means_disabled_and_silent() -> None:
    bk = Backoffice.from_env("tidewater", env={})
    assert not bk.enabled
    assert bk.book_receipt(receipt(), "Northwind") == []
    assert bk.book("update_crm", _facts()) is False


def test_token_without_tools_stays_off(caplog: pytest.LogCaptureFixture) -> None:
    bk = Backoffice.from_env("tidewater", env={"TIDEWATER_ZAPIER_MCP_TOKEN": "tok"})
    assert not bk.enabled
    assert "maps no usable action" in caplog.text


def test_missing_mcp_extra_turns_the_feature_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def no_extra(url: str, token: str) -> None:
        raise ImportError("mcp")

    monkeypatch.setattr(bo, "ZapierMcpSession", no_extra)
    env = {"TIDEWATER_ZAPIER_MCP_TOKEN": "tok", "TIDEWATER_ZAPIER_TOOLS": '{"update_crm": "hubspot_update_deal"}'}
    bk = Backoffice.from_env("tidewater", env=env)
    assert not bk.enabled
    assert "zapier extra is not installed" in caplog.text
    assert "tok" not in caplog.text


def test_token_and_tools_build_a_session_and_never_log_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    made: list[tuple[str, str]] = []

    def fake_session(url: str, token: str) -> FakeSession:
        made.append((url, token))
        return FakeSession()

    monkeypatch.setattr(bo, "ZapierMcpSession", fake_session)
    env = {"NORTHWIND_ZAPIER_MCP_TOKEN": "secret-token-123", "NORTHWIND_ZAPIER_TOOLS": '{"record_bill": "qb_bill"}'}
    bk = Backoffice.from_env("northwind", env=env)
    assert bk.enabled and bk.actions == frozenset({"record_bill"})
    assert made == [(bo.DEFAULT_URL, "secret-token-123")]
    assert "secret-token-123" not in caplog.text


def test_http_url_is_refused_unless_localhost() -> None:
    with pytest.raises(BackofficeConfigError):
        bo.validate_url("http://mcp.example.com/connect")
    assert bo.validate_url("http://127.0.0.1:9/connect") == "http://127.0.0.1:9/connect"


# ----------------------------------------------------------------------------- facts and arguments


def test_settled_receipt_books_invoice_and_crm_with_code_built_arguments() -> None:
    session = FakeSession()
    bk = make("tidewater", session)
    booked = bk.book_receipt(receipt("settled"), "Northwind Outfitters")
    wait(bk)
    assert booked == ["record_invoice", "update_crm"]
    by_tool = dict(session.calls)
    assert by_tool["quickbooks_create_invoice"] == {
        "deal_id": "deal_ab12",
        "customer": "Northwind Outfitters",
        "amount": "42.00",
        "currency": "USD",
        "memo": "Trustee deal deal_ab12; PayPal capture CAP-1",
    }
    assert by_tool["hubspot_update_deal"]["stage"] == "closed_won"


def test_refunded_receipt_books_no_invoice_and_marks_crm_lost() -> None:
    session = FakeSession()
    bk = make("tidewater", session)
    assert bk.book_receipt(receipt("voided", capture=None), "Northwind") == ["update_crm"]
    wait(bk)
    expected = build_arguments("update_crm", _facts(outcome="voided", counterparty="Northwind"))
    assert session.calls == [("hubspot_update_deal", expected)]
    assert session.calls[0][1]["stage"] == "closed_lost"


def test_partial_settlement_books_the_captured_amount_not_the_contract_price() -> None:
    partial = receipt("settled", amount="30.00")
    partial["contract_amount"] = "50.00"
    partial["settlement"] = {"captured": "30.00", "released": "20.00"}
    facts = facts_from_receipt(partial, "Northwind")
    assert facts.amount == "30.00"
    session = FakeSession()
    bk = make("northwind", session)
    bk.book_receipt(partial, "Tidewater Data Co.")
    wait(bk)
    args = dict(session.calls)["quickbooks_create_bill"]
    assert args["amount"] == "30.00"
    assert args["status"] == "paid"


def test_northwind_books_bill_and_finance_note_for_a_refund() -> None:
    session = FakeSession()
    bk = make("northwind", session)
    assert bk.book_receipt(receipt("voided", capture=None), "Tidewater") == ["record_bill", "post_finance_note"]
    wait(bk)
    bill = dict(session.calls)["quickbooks_create_bill"]
    assert bill["status"] == "voided"
    assert bill["paypal_capture_id"] == "none"


def test_non_settlement_receipt_is_not_booked(caplog: pytest.LogCaptureFixture) -> None:
    session = FakeSession()
    bk = make("tidewater", session)
    assert bk.book_receipt({"outcome": "pending", "deal_id": "d"}, "x") == []
    assert "not bookable" in caplog.text
    assert session.calls == []


def test_arguments_carry_only_ids_amounts_and_fixed_text() -> None:
    facts = _facts(outcome="settled")
    for action in ("record_invoice", "update_crm", "record_bill", "post_finance_note"):
        args = build_arguments(action, facts)
        assert "delivery_sha256" not in str(args)
        assert "signature" not in str(args)


def test_step_up_notice_carries_the_approval_link_and_no_tool_choice() -> None:
    session = FakeSession()
    bk = make("northwind", session)
    facts = _facts(outcome="settled", approve_url="https://paypal.example/approve?t=1")
    assert bk.notify_step_up(facts) is True
    wait(bk)
    name, args = session.calls[0]
    assert name == "gmail_send_email"
    assert args["approve_url"] == "https://paypal.example/approve?t=1"
    assert "approve or decline" in args["text"].lower()


# ----------------------------------------------------------------------------- idempotency


def test_repeated_push_books_once() -> None:
    session = FakeSession()
    bk = make("tidewater", session)
    first = bk.book_receipt(receipt(), "Northwind")
    second = bk.book_receipt(receipt(), "Northwind")
    wait(bk)
    assert first == ["record_invoice", "update_crm"]
    assert second == []
    assert len(session.calls) == 2


# ------------------------------------------------------------------------ failure isolation


def test_failing_zapier_never_raises_into_the_deal_and_is_recorded() -> None:
    session = FakeSession(fail=True)
    seen: list[bo.BookingResult] = []
    sleeps: list[float] = []
    bk = Backoffice(
        "tidewater",
        session=session,
        tools=TW_TOOLS,
        on_result=seen.append,
        retries=2,
        sleep=sleeps.append,
    )
    assert bk.book_receipt(receipt(), "Northwind") == ["record_invoice", "update_crm"]
    wait(bk)
    assert len(session.calls) == 6  # 3 attempts per action
    assert all(not r.ok and r.error == "RuntimeError" for r in seen)
    assert len(seen) == 2
    assert sleeps == [1.0, 2.0, 1.0, 2.0]
    assert "zapier is down" not in repr(seen)  # error text is reduced to the exception type


def test_full_queue_drops_new_bookings_without_blocking() -> None:
    gate = threading.Event()

    class Blocking(FakeSession):
        def call_tool(self, name: str, arguments: dict[str, Any]) -> None:
            gate.wait(5)
            super().call_tool(name, arguments)

    session = Blocking()
    bk = make("tidewater", session, max_queue=1)
    accepted = [bk.book("update_crm", _facts(deal_id=f"deal_{i}")) for i in range(5)]
    gate.set()
    wait(bk)
    assert accepted.count(True) <= 2
    assert accepted.count(False) >= 3


# ------------------------------------------------------------------------ Northwind hooks


def test_northwind_step_up_hook_notifies_only_when_intent_and_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from trustee.a2a.models import TaskState
    from trustee.network.northwind import Northwind

    monkeypatch.delenv("NORTHWIND_ZAPIER_MCP_TOKEN", raising=False)
    session = FakeSession()
    bk = make("northwind", session)
    nw = Northwind.__new__(Northwind)  # skip the full agent build: only the hook is under test
    nw.backoffice = bk
    nw.instruction = "buy the Q3 dataset"
    contract = SimpleNamespace(price=Decimal("12.50"), currency="USD")
    task = SimpleNamespace(
        status=SimpleNamespace(state=TaskState.AUTH_REQUIRED),
        metadata={"trustee": {"approveUrl": "https://paypal.example/approve?x=2"}},
    )
    nw._notify_step_up("deal_zz", "Tidewater Data Co.", contract, task)  # type: ignore[arg-type]
    wait(bk)
    assert session.calls[0][0] == "gmail_send_email"
    assert session.calls[0][1]["amount"] == "12.50"
    assert session.calls[0][1]["approve_url"] == "https://paypal.example/approve?x=2"


def test_hosted_northwind_without_env_keeps_hops_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from trustee.network.northwind import create_northwind

    for name in ("NORTHWIND_ZAPIER_MCP_TOKEN", "NORTHWIND_ZAPIER_TOOLS", "TIDEWATER_ZAPIER_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    nw = create_northwind()
    assert not nw.backoffice.enabled


def test_tidewater_books_the_settlement_it_is_pushed(monkeypatch: pytest.MonkeyPatch) -> None:
    from trustee.a2a.models import TaskState
    from trustee.network import tidewater as tw

    session = FakeSession()
    bk = make("tidewater", session)
    agent = tw.Tidewater.__new__(tw.Tidewater)  # only the booking hook is under test
    agent.backoffice = bk
    agent.deals = {}
    agent.peers = {}
    monkeypatch.setattr(tw, "artifact_data", lambda task, name: receipt())
    task = SimpleNamespace(id="deal_ab12", status=SimpleNamespace(state=TaskState.COMPLETED))
    agent._book_settlement(task)  # type: ignore[arg-type]
    wait(bk)
    assert sorted(name for name, _ in session.calls) == ["hubspot_update_deal", "quickbooks_create_invoice"]


# ------------------------------------------------------------------------------- helpers


def _facts(**kw: Any) -> Facts:
    base: dict[str, Any] = {
        "deal_id": "deal_ab12",
        "counterparty": "Northwind Outfitters",
        "amount": "42.00",
        "currency": "USD",
        "outcome": "settled",
        "capture_id": "CAP-1",
    }
    base.update(kw)
    return Facts(**base)
