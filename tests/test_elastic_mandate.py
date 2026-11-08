"""Mandate reputation rules, the Elastic setup script and the A2A ledger analyst client."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_mandate import contract as mandate_contract
from test_platform_api import World

from trustee import analyst as analyst_mod
from trustee.analyst import AnalystError, LedgerAnalyst
from trustee.elastic.config import ElasticConfigError
from trustee.identity import AgentKey, PrincipalRegistry, canonical_json
from trustee.mandate import Mandate, MandateEngine, demo_mandates
from trustee.platform.wire import MandateIn, mandate_to_dict
from trustee.reputation import Reputation

# ------------------------------------------------------------------- mandate rules


class FixedRep:
    def __init__(self, **by_agent: Reputation):
        self.by_agent = by_agent
        self.calls: list[tuple[str, str | None]] = []

    def reputation(self, agent_id: str, exclude_deal: str | None = None) -> Reputation:
        self.calls.append((agent_id, exclude_deal))
        return self.by_agent[agent_id]


def engine_with(provider: Any, **buyer_rules: Any) -> MandateEngine:
    ms = demo_mandates()
    b = ms["buyer-agent-1"]
    ms["buyer-agent-1"] = Mandate(**{**b.__dict__, **buyer_rules})
    return MandateEngine(ms, reputation=provider)


def rep(deals: int, disputed: int = 0) -> Reputation:
    rate = disputed / deals if deals else 0.0
    return Reputation("seller-agent-1", deals_total=deals, disputed=disputed, dispute_rate=rate)


def test_rules_pass_and_show_up_in_the_report():
    provider = FixedRep(**{"seller-agent-1": rep(10, 1)})
    engine = engine_with(provider, min_counterparty_deals=5, max_counterparty_dispute_rate=Decimal("0.2"))
    report = engine.check_deal(mandate_contract("d7"))
    assert report.ok
    summary = report.summarize()
    assert "deals 10 >= 5" in summary and "dispute rate 0.1 <= 0.2" in summary
    assert provider.calls == [("seller-agent-1", "d7")]


def test_rules_fail_with_reasons():
    engine = engine_with(
        FixedRep(**{"seller-agent-1": rep(2, 1)}),
        min_counterparty_deals=5,
        max_counterparty_dispute_rate=Decimal("0.2"),
    )
    report = engine.check_deal(mandate_contract())
    assert not report.ok
    assert any("has 2 deals" in v for v in report.violations)
    assert any("dispute rate 0.5 exceeds" in v for v in report.violations)


def test_rule_without_provider_fails_closed():
    report = engine_with(None, min_counterparty_deals=1).check_deal(mandate_contract())
    assert not report.ok and "no reputation source is configured" in report.summarize()


def test_provider_error_fails_closed():
    class Boom:
        def reputation(self, agent_id: str, exclude_deal: str | None = None) -> Reputation:
            raise RuntimeError("secret detail")

    report = engine_with(Boom(), max_counterparty_dispute_rate=Decimal("0.5")).check_deal(mandate_contract())
    assert not report.ok and "reputation unavailable (lookup failed (RuntimeError))" in report.summarize()
    assert "secret detail" not in report.summarize()


def test_mandates_without_rules_never_consult_the_provider():
    provider = FixedRep()
    engine = engine_with(provider)
    assert engine.check_deal(mandate_contract()).summarize() == "mandates OK"
    assert provider.calls == []
    assert MandateEngine(demo_mandates()).check_deal(mandate_contract()).summarize() == "mandates OK"


def test_recheck_skips_reputation_rules():
    provider = FixedRep(**{"seller-agent-1": rep(0)})
    engine = engine_with(provider, min_counterparty_deals=5)
    assert not engine.check_deal(mandate_contract()).ok
    provider.calls.clear()
    assert engine.recheck(mandate_contract()).ok  # funds already held: a drifting record must not strand them
    assert provider.calls == []


def test_mandate_signed_before_the_new_fields_still_verifies():
    old_style_body = {
        "agent_id": "buyer-agent-1",
        "principal": "demo-principal",
        "max_amount": Decimal("500.00"),
        "allowed_categories": frozenset({"code"}),
        "allowed_counterparties": None,
        "valid_until": None,
        "grants": "spend",
        "notes": "",
        "max_total": None,
        "revoked": False,
        "version": 0,
    }
    m = Mandate(
        agent_id="buyer-agent-1",
        principal="demo-principal",
        max_amount=Decimal("500.00"),
        allowed_categories=frozenset({"code"}),
    )
    assert m.signing_bytes() == canonical_json(old_style_body)
    key = AgentKey.generate()
    registry = PrincipalRegistry()
    registry.register("demo-principal", key.public_key_b64)
    stored = Mandate(**{**m.__dict__, "principal_signature": key.sign(canonical_json(old_style_body))})
    assert registry.verify("demo-principal", stored.principal_signature, stored.signing_bytes())
    ruled = Mandate(**{**m.__dict__, "min_counterparty_deals": 3})
    assert b"min_counterparty_deals" in ruled.signing_bytes() and b"max_counterparty" not in ruled.signing_bytes()


def test_mandate_rules_over_http():
    w = World().setup()
    k = w.keys
    ruled = Mandate(
        agent_id="buyer",
        principal="org-b",
        max_amount=Decimal("500.00"),
        allowed_categories=frozenset({"code"}),
        allowed_counterparties=frozenset({"seller"}),
        grants="spend",
        version=1,
        min_counterparty_deals=1,
        max_counterparty_dispute_rate=Decimal("0.25"),
    )
    assert w.mandate(ruled, k["org-b"]).status_code == 201
    refused = w.open(w.contract("d1"))
    assert refused.status_code == 403
    assert any("has 0 deals" in v for v in refused.json()["violations"])
    assert w.paypal.total == 0
    tape = [e for e in w.service.ledger.tail("d1") if e.kind == "mandate"]
    assert tape and "has 0 deals" in tape[0].payload["report"]
    # Editing the rule after signing breaks the principal's signature.
    tampered = mandate_to_dict(Mandate(**{**ruled.sign(k["org-b"]).__dict__, "version": 2}))
    tampered["min_counterparty_deals"] = 0
    assert w.post("/v1/mandates", tampered).status_code == 401
    relaxed = Mandate(**{**ruled.__dict__, "version": 3, "min_counterparty_deals": 0})
    assert w.mandate(relaxed, k["org-b"]).status_code == 201
    assert w.open(w.contract("d2")).status_code == 201
    report = [e for e in w.service.ledger.tail("d2") if e.kind == "mandate"][0].payload["report"]
    assert "dispute rate 0.0 <= 0.25" in report


def test_wire_model_accepts_and_bounds_the_new_fields():
    base = {"agent_id": "a", "principal": "p", "max_amount": "1"}
    body = MandateIn.model_validate({**base, "min_counterparty_deals": 2, "max_counterparty_dispute_rate": "0.1"})
    assert body.min_counterparty_deals == 2 and body.max_counterparty_dispute_rate == Decimal("0.1")
    with pytest.raises(ValueError):
        MandateIn.model_validate({**base, "max_counterparty_dispute_rate": "2"})
    assert MandateIn.model_validate(base).min_counterparty_deals is None


# ------------------------------------------------------------------ setup script


def load_setup() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "elastic_setup.py"
    spec = importlib.util.spec_from_file_location("elastic_setup", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_setup_dry_run_prints_requests_and_sends_nothing(capsys: pytest.CaptureFixture[str], monkeypatch: Any):
    mod = load_setup()
    monkeypatch.setenv("ELASTICSEARCH_URL", "https://es.example.com")
    monkeypatch.setenv("KIBANA_URL", "https://kb.example.com")
    monkeypatch.delenv("ELASTIC_API_KEY", raising=False)

    def no_network(*_a: Any, **_k: Any) -> None:
        raise AssertionError("dry run must not send requests")

    monkeypatch.setattr(httpx.Client, "request", no_network)
    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "PUT https://es.example.com/_index_template/trustee-ledger" in out
    for tool_id in (
        "trustee-deal-history",
        "trustee-spend",
        "trustee-void-reasons",
        "trustee-agent-reputation",
        "trustee-money-incidents",
        "trustee-contradicted-claims",
    ):
        assert tool_id in out
    assert "POST https://kb.example.com/api/agent_builder/agents" in out
    assert "trustee-ledger-analyst" in out and "cannot move, refund, capture or void money" in out
    assert "https://kb.example.com/api/agent_builder/a2a/trustee-ledger-analyst.json" in out
    assert '"type": "string"' in out
    assert mod.main(["--dry-run", "--legacy-param-types"]) == 0
    assert '"type": "keyword"' in capsys.readouterr().out


def test_setup_is_idempotent_and_updates_existing_objects():
    mod = load_setup()
    calls: list[tuple[str, str]] = []
    existing = {"/api/agent_builder/tools", "/api/agent_builder/agents"}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "ApiKey K" and req.headers["kbn-xsrf"] == "true"
        if req.method == "POST" and req.url.path in existing:
            return httpx.Response(409, json={"message": "exists"})
        if req.method == "PUT" and req.url.path == "/trustee-ledger":
            return httpx.Response(400, json={"error": {"type": "resource_already_exists_exception"}})
        return httpx.Response(200, json={})

    reqs = mod.build_requests("https://es.example.com", "https://kb.example.com", "trustee-ledger")
    assert mod.run(reqs, "K", httpx.Client(transport=httpx.MockTransport(handler))) == 0
    puts = [p for m, p in calls if m == "PUT"]
    assert "/api/agent_builder/tools/trustee-spend" in puts
    assert "/api/agent_builder/agents/trustee-ledger-analyst" in puts
    assert all("id" not in r.update[1] for r in reqs if r.update)


def test_setup_reports_failures():
    mod = load_setup()
    reqs = mod.build_requests("https://es.example.com", "https://kb.example.com", "trustee-ledger")
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403, text="no")))
    assert mod.run(reqs, "K", client) == len(reqs)


def test_every_tool_query_is_parameterized():
    mod = load_setup()
    for tool in mod.tools("trustee-ledger"):
        query = tool["configuration"]["query"]
        for name in tool["configuration"]["params"]:
            assert f"?{name}" in query, (tool["id"], name)


# ----------------------------------------------------------------------- analyst


def kibana(handler: Any) -> LedgerAnalyst:
    return LedgerAnalyst("https://kb.example.com", "K", http=httpx.Client(transport=httpx.MockTransport(handler)))


def rpc_result(result: dict[str, Any]) -> Any:
    return lambda _r: httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result})


def test_analyst_sends_message_send_and_returns_text():
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(req.url), req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        part = {"kind": "text", "text": "Refunded: no delivery."}
        result = {"kind": "message", "role": "agent", "parts": [part]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": seen["body"]["id"], "result": result})

    assert kibana(handler).ask("why was deal d1 refunded?") == "Refunded: no delivery."
    assert seen["url"] == "https://kb.example.com/api/agent_builder/a2a/trustee-ledger-analyst"
    assert seen["auth"] == "ApiKey K"
    assert seen["body"]["method"] == "message/send"
    assert seen["body"]["params"]["message"]["parts"] == [{"kind": "text", "text": "why was deal d1 refunded?"}]


def test_analyst_reads_a_task_result():
    task = {
        "kind": "task",
        "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "text", "text": "Deal "}, {"kind": "text", "text": "d1 settled."}]}],
    }
    assert kibana(rpc_result(task)).ask("q") == "Deal d1 settled."
    assert analyst_mod.answer_text({"status": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}}) == "hi"


def test_analyst_errors_are_clear():
    with pytest.raises(AnalystError, match="HTTP 403"):
        kibana(lambda _r: httpx.Response(403, json={"message": "forbidden"})).ask("q")
    err = {"jsonrpc": "2.0", "id": "1", "error": {"message": "boom"}}
    with pytest.raises(AnalystError, match="boom"):
        kibana(lambda _r: httpx.Response(200, json=err)).ask("q")
    with pytest.raises(AnalystError, match="no text"):
        kibana(rpc_result({"kind": "task"})).ask("q")
    with pytest.raises(ElasticConfigError):
        LedgerAnalyst("http://kb.example.com", "K")


def test_analyst_card_and_cli(capsys: pytest.CaptureFixture[str], monkeypatch: Any):
    analyst = kibana(lambda r: httpx.Response(200, json={"name": "Trustee Ledger Analyst", "url": str(r.url)}))
    assert analyst.card()["url"] == "https://kb.example.com/api/agent_builder/a2a/trustee-ledger-analyst.json"
    monkeypatch.delenv("KIBANA_URL", raising=False)
    assert analyst_mod.main(["why", "x?"]) == 1
    assert "KIBANA_URL" in capsys.readouterr().err


# ------------------------------------------------------------- accountability tools


def _shipped_docs(world: World) -> list[dict[str, Any]]:
    """The documents the Elastic sink would index for everything on this world's ledger."""
    from trustee.elastic.mapping import build_doc, deal_facts

    facts: dict[str, dict[str, Any]] = {}
    docs = []
    for seq, ev in enumerate(world.service.ledger.tail(), start=1):
        found = deal_facts(ev)
        if found is not None and ev.deal_id:
            facts[ev.deal_id] = found
        docs.append(build_doc(ev, "platform", seq, facts.get(ev.deal_id or "")))
    return docs


def test_incident_rows_carry_the_fields_the_queries_filter_on():
    """The ES|QL cannot run here, so check its inputs: rows from the real code paths have the kind, op, decision and
    buyer values that money_incidents_query and contradicted_claims_query select on."""
    from test_platform_api import GOOD_HASH

    from trustee.elastic import queries

    w = World().setup()
    w.open(w.contract())
    w.deliver("deal-1", content_hash=GOOD_HASH)
    w.attest("deal-1", verdict="reject", evidence={"sha256_check": "mismatch"})
    docs = _shipped_docs(w)
    contradicted = [d for d in docs if d["kind"] == "note" and d.get("op") == "evidence_check"]
    assert len(contradicted) == 1 and contradicted[0]["buyer"] == "buyer" and contradicted[0]["deal_id"] == "deal-1"
    upheld = [d for d in docs if d["kind"] == "ruling" and d.get("decision") == "uphold"]
    assert upheld and "contradicts the signed record" in upheld[0]["detail"]
    incidents = queries.money_incidents_query("trustee-ledger")
    for needle in ('"evidence_check"', 'decision == "uphold"', '"intent_not_recorded"', 'kind == "paypal_alert"'):
        assert needle in incidents
    assert 'op == "evidence_check"' in queries.contradicted_claims_query("trustee-ledger")


def test_storage_refusal_rows_are_incidents(monkeypatch: pytest.MonkeyPatch):
    w = World().setup()

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(w.store, "upsert_deal", broken)
    assert w.open(w.contract()).status_code == 503
    refused = [d for d in _shipped_docs(w) if d["kind"] == "note" and d.get("op") == "intent_not_recorded"]
    assert refused and refused[0]["buyer"] == "buyer" and "no PayPal call made" in refused[0]["detail"]


def test_incident_queries_reject_unsafe_index_names():
    from trustee.elastic import queries

    for build in (queries.money_incidents_query, queries.contradicted_claims_query):
        with pytest.raises(ValueError):
            build("ledger | DROP x")
