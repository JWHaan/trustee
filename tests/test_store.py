"""Tests for the durable SQLite store."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from trustee.ledger import Ledger
from trustee.store import Store, StoreConflict, StoreError


def test_schema_is_idempotent_and_data_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "trustee.db"
    with Store(db) as s:
        assert s.schema_version() == 1
        s.put_agent("a1", "pk1", "p1", ["buyer"])
        s.upsert_deal("d1", "AUTHORIZED", {"amount": "10.00"}, {"step": 1})
    with Store(db) as s:  # re-open runs migration again; must not fail or reset
        assert s.schema_version() == 1
        agent = s.get_agent("a1")
        deal = s.get_deal("d1")
        assert agent is not None and agent["roles"] == ["buyer"]
        assert deal is not None and deal["state"] == "AUTHORIZED"


def test_future_schema_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '99')")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError):
        Store(db)


def test_agent_key_swap_refused_same_key_updates_other_fields() -> None:
    with Store() as s:
        s.put_agent("a1", "pk1", "p1", ["buyer"])
        with pytest.raises(StoreConflict):
            s.put_agent("a1", "pk2", "p1", ["buyer"])
        s.put_agent("a1", "pk1", "p2", ["buyer", "seller"], paypal_email="x@example.com", metadata={"k": 1})
        agent = s.get_agent("a1")
        assert agent is not None
        assert agent["public_key"] == "pk1"
        assert agent["principal"] == "p2"
        assert agent["roles"] == ["buyer", "seller"]
        assert agent["paypal_email"] == "x@example.com"
        assert agent["metadata"] == {"k": 1}
        assert s.get_agent("missing") is None
        assert [a["agent_id"] for a in s.list_agents()] == ["a1"]


def test_principal_key_swap_refused_same_key_is_noop() -> None:
    with Store() as s:
        s.put_principal("p1", "pk1")
        s.put_principal("p1", "pk1")
        with pytest.raises(StoreConflict):
            s.put_principal("p1", "pk2")
        principal = s.get_principal("p1")
        assert principal is not None and principal["public_key"] == "pk1"
        assert s.get_principal("nope") is None


def test_mandate_put_get_revoke() -> None:
    with Store() as s:
        assert s.get_mandate("a1") is None
        s.put_mandate("a1", "p1", {"max_amount": "50.00"})
        m = s.get_mandate("a1")
        assert m is not None
        assert m["body"] == {"max_amount": "50.00"}
        assert m["revoked"] is False
        assert s.revoke_mandate("a1") is True
        revoked = s.get_mandate("a1")
        assert revoked is not None and revoked["revoked"] is True
        assert s.revoke_mandate("nobody") is False
        s.put_mandate("a1", "p1", {"max_amount": "20.00"})  # re-issuing clears the revocation
        reissued = s.get_mandate("a1")
        assert reissued is not None and reissued["revoked"] is False


def test_deal_upsert_get_and_list_by_state() -> None:
    with Store() as s:
        s.upsert_deal("d1", "NEGOTIATING", {"amount": "5"}, {"n": 1})
        s.upsert_deal("d2", "AUTHORIZED", None, {"n": 2})
        s.upsert_deal("d3", "SETTLED", None, {"n": 3})
        s.upsert_deal("d1", "AUTHORIZED", None, {"n": 4})  # None contract keeps the stored one

        d1 = s.get_deal("d1")
        assert d1 is not None
        assert d1["state"] == "AUTHORIZED"
        assert d1["contract"] == {"amount": "5"}
        assert d1["snapshot"] == {"n": 4}
        assert s.get_deal("missing") is None

        authorized = s.list_deals(states=["AUTHORIZED"])
        assert {d["deal_id"] for d in authorized} == {"d1", "d2"}
        assert s.list_deals(states=[]) == []
        assert len(s.list_deals()) == 3


def test_list_deals_is_newest_first() -> None:
    with Store() as s:
        for deal_id in ("d1", "d2", "d3"):
            s.upsert_deal(deal_id, "AUTHORIZED", None, {})
        assert [d["deal_id"] for d in s.list_deals()] == ["d3", "d2", "d1"]


def test_open_deals_excludes_settled_and_voided() -> None:
    with Store() as s:
        s.upsert_deal("open", "AUTHORIZED", None, {})
        s.upsert_deal("done", "SETTLED", None, {})
        s.upsert_deal("gone", "VOIDED", None, {})
        assert [d["deal_id"] for d in s.open_deals()] == ["open"]


def test_events_ascending_with_paging_and_deal_filter() -> None:
    with Store() as s:
        for i in range(5):
            s.append_event("2026-01-01T00:00:00", "d1", "trustee", "state", {"i": i})
        s.append_event("2026-01-01T00:00:01", "d2", "trustee", "state", {"i": 99})

        all_rows = s.events()
        assert [e["seq"] for e in all_rows] == sorted(e["seq"] for e in all_rows)
        assert len(all_rows) == 6

        d1 = s.events(deal_id="d1")
        assert [e["payload"]["i"] for e in d1] == [0, 1, 2, 3, 4]

        page = s.events(deal_id="d1", after_seq=d1[1]["seq"], limit=2)
        assert [e["payload"]["i"] for e in page] == [2, 3]


def test_attach_persists_ledger_events_and_unsubscribe_stops() -> None:
    ledger = Ledger()
    with Store() as s:
        unsubscribe = s.attach(ledger)
        ledger.append("d1", "trustee", "state", to="AUTHORIZED")
        rows = s.events()
        assert len(rows) == 1
        assert rows[0]["deal_id"] == "d1"
        assert rows[0]["kind"] == "state"
        assert rows[0]["payload"] == {"to": "AUTHORIZED"}

        unsubscribe()
        ledger.append("d1", "trustee", "state", to="SETTLED")
        assert len(s.events()) == 1


def test_closed_store_does_not_raise_into_ledger_append(caplog: pytest.LogCaptureFixture) -> None:
    ledger = Ledger()
    s = Store()
    s.attach(ledger)
    s.close()
    with caplog.at_level(logging.ERROR, logger="trustee.store"):
        ev = ledger.append("d1", "buyer", "paypal", op="capture")  # must not raise
    assert ev.kind == "paypal"
    assert len(ledger) == 1
    assert "failed to persist" in caplog.text


def test_webhook_dedupe() -> None:
    with Store() as s:
        assert s.record_webhook("WH-1", "PAYMENT.CAPTURE.COMPLETED", "CAP-1", {"a": 1}) is True
        assert s.record_webhook("WH-1", "PAYMENT.CAPTURE.COMPLETED", "CAP-1", {"a": 1}) is False
        assert s.record_webhook("WH-2", "PAYMENT.CAPTURE.DENIED", None, {}) is True


def test_concurrent_writers_lose_nothing() -> None:
    with Store() as s:

        def writer(n: int) -> None:
            for i in range(50):
                s.append_event("2026-01-01T00:00:00", f"d{n}", "trustee", "note", {"i": i})

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(s.events(limit=10_000)) == 400
