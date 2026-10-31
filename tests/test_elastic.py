"""Elastic: config, ledger shipping and reputation (local and ES|QL).

No real network: every Elastic endpoint is an httpx.MockTransport and backoff sleeps are injected.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from test_platform_api import World

from trustee.elastic import queries
from trustee.elastic.config import (
    ElasticConfig,
    ElasticConfigError,
    arena_elastic_enabled,
    config_from_env,
    url_from_cloud_id,
    validate_url,
)
from trustee.elastic.mapping import build_doc, scrub
from trustee.elastic.reputation import ElasticReputation, ElasticReputationError, parse_reputation, reputation_from_env
from trustee.elastic.sink import ElasticSink, sink_from_env
from trustee.ledger import Event, Ledger
from trustee.reputation import Reputation

CFG = ElasticConfig(url="https://es.example.com", api_key="KEY123", index="trustee-ledger")
BASE = {"ELASTICSEARCH_URL": "https://es.example.com", "ELASTIC_API_KEY": "k"}


# ----------------------------------------------------------------------------- config


def test_url_must_be_https_except_localhost():
    assert validate_url("https://es.example.com/") == "https://es.example.com"
    assert validate_url("http://localhost:9200") == "http://localhost:9200"
    assert validate_url("http://127.0.0.1:9200") == "http://127.0.0.1:9200"
    for bad in ("http://es.example.com", "ftp://x", "es.example.com", ""):
        with pytest.raises(ElasticConfigError):
            validate_url(bad)


def test_config_from_env_variants():
    assert config_from_env({}) is None
    with pytest.raises(ElasticConfigError):
        config_from_env({"ELASTICSEARCH_URL": "https://es.example.com"})  # no key
    cfg = config_from_env(BASE)
    assert cfg is not None and cfg.index == "trustee-ledger"
    cfg = config_from_env({**BASE, "ELASTIC_LEDGER_INDEX": "my-ledger"})
    assert cfg is not None and cfg.index == "my-ledger"
    with pytest.raises(ElasticConfigError):
        config_from_env({"ELASTICSEARCH_URL": "http://es.example.com", "ELASTIC_API_KEY": "k"})


def test_cloud_id_is_decoded():
    cid = "dep:" + base64.b64encode(b"us-east-1.aws.found.io:443$abc123$def456").decode()
    assert url_from_cloud_id(cid) == "https://abc123.us-east-1.aws.found.io"
    cfg = config_from_env({"ELASTIC_CLOUD_ID": cid, "ELASTIC_API_KEY": "k"})
    assert cfg is not None and cfg.url == "https://abc123.us-east-1.aws.found.io"
    with pytest.raises(ElasticConfigError):
        url_from_cloud_id("dep:!!!")


def test_arena_flag():
    assert not arena_elastic_enabled({})
    assert arena_elastic_enabled({"TRUSTEE_ELASTIC_ARENA": "1"})


# ------------------------------------------------------------------------------- sink


class Es:
    """A recording _bulk endpoint. `plan` pops one response step per request (status, exception or callable)."""

    def __init__(self, plan: list[Any] | None = None):
        self.requests: list[httpx.Request] = []
        self.plan = list(plan or [])
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        step = self.plan.pop(0) if self.plan else 200
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx.Response(step, json={"errors": False, "items": []})
        return step(req)

    def _lines(self) -> list[list[dict[str, Any]]]:
        return [[json.loads(x) for x in r.content.decode().strip().split("\n")] for r in self.requests]

    def docs(self) -> list[dict[str, Any]]:
        return [d for lines in self._lines() for d in lines[1::2]]

    def actions(self) -> list[dict[str, Any]]:
        return [a for lines in self._lines() for a in lines[0::2]]


def make_sink(es: Es, **kw: Any) -> tuple[ElasticSink, Ledger]:
    sink = ElasticSink(CFG, source=kw.pop("source", "platform"), http=es.client, sleep=lambda _s: None, **kw)
    ledger = Ledger()
    sink.attach(ledger)
    return sink, ledger


def test_sink_bulk_indexes_events_with_deal_parties():
    es = Es()
    sink, ledger = make_sink(es)
    ledger.append("d1", "trustee", "msg", event="contract_formed", buyer="b", seller="s", price="12.50 USD")
    ledger.append("d1", "trustee", "state", event="transition", state_from="ESCROWED", state_to="DELIVERED", note="ok")
    assert sink.flush()
    req = es.requests[0]
    assert str(req.url) == "https://es.example.com/_bulk"
    assert req.headers["authorization"] == "ApiKey KEY123"
    assert req.headers["content-type"] == "application/x-ndjson"
    assert req.content.endswith(b"\n")
    docs = es.docs()
    assert [d["kind"] for d in docs] == ["msg", "state"]
    assert docs[1]["buyer"] == "b" and docs[1]["seller"] == "s" and docs[1]["amount"] == 12.5
    assert docs[1]["currency"] == "USD" and docs[1]["state_to"] == "DELIVERED" and docs[1]["detail"] == "ok"
    assert docs[1]["@timestamp"].endswith("Z") and docs[1]["source"] == "platform"
    assert all(a["create"]["_index"] == "trustee-ledger" for a in es.actions())
    assert len({a["create"]["_id"] for a in es.actions()}) == 2
    sink.close()


def test_sink_batches_many_events():
    es = Es()
    sink, ledger = make_sink(es, batch_size=10)
    with sink._cv:  # hold the worker so events pile up into batches
        for i in range(25):
            ledger.append("d", "trustee", "note", op="x", explanation=str(i))
    assert sink.flush()
    assert len(es.docs()) == 25
    assert all(len(lines) <= 20 for lines in es._lines())
    sink.close()


def test_sink_retries_transient_failures_then_succeeds():
    es = Es([503, httpx.ConnectError("boom"), 200])
    sink, ledger = make_sink(es)
    ledger.append("d", "trustee", "note", op="x")
    assert sink.flush()
    assert len(es.requests) == 3
    assert sink.delivered == 1 and sink.dropped == 0
    sink.close()


def test_sink_gives_up_after_retries_and_stays_alive():
    es = Es([500, 500, 500, 500, 200])
    sink, ledger = make_sink(es, retries=3)
    ledger.append("d", "trustee", "note", op="first")
    assert sink.flush()
    assert sink.dropped == 1
    ledger.append("d", "trustee", "note", op="second")
    assert sink.flush()
    assert sink.delivered == 1
    sink.close()


def test_sink_does_not_retry_auth_errors():
    es = Es([401, 200])
    sink, ledger = make_sink(es)
    ledger.append("d", "trustee", "note", op="x")
    assert sink.flush()
    assert len(es.requests) == 1 and sink.dropped == 1
    sink.close()


def test_sink_retries_only_failed_items():
    def partial(_req: httpx.Request) -> httpx.Response:
        items = [{"create": {"status": 201}}, {"create": {"status": 429}}, {"create": {"status": 409}}]
        return httpx.Response(200, json={"errors": True, "items": items})

    es = Es([partial, 200])
    sink, ledger = make_sink(es)
    with sink._cv:
        for i in range(3):
            ledger.append("d", "trustee", "note", op=str(i))
    assert sink.flush()
    assert len(es.requests) == 2
    assert len(es._lines()[1]) == 2  # one action + one doc
    assert sink.delivered == 3
    sink.close()


def test_sink_queue_is_bounded_and_drops_oldest():
    es = Es()
    sink, _ledger = make_sink(es, max_queue=3)
    with sink._cv:  # keep the worker from draining while we overfill
        for i in range(6):
            sink._enqueue((f"id{i}", {"n": i}))
    assert sink.flush()
    assert sink.dropped >= 3
    sink.close()


def test_sink_never_breaks_the_ledger():
    es = Es([httpx.ConnectError("down")] * 10)
    sink, ledger = make_sink(es, retries=0)
    ledger.append("d", "trustee", "note", op="x")  # must not raise
    assert sink.flush()
    sink.close()
    ledger.append("d", "trustee", "note", op="after close")  # no subscriber left


def test_documents_carry_no_secrets():
    ev = Event(
        ts="2026-01-01T10:00:00",
        deal_id="d",
        actor="a",
        kind="identity",
        payload={"signature": "SIG", "nested": {"api_key": "K", "ok": 1}, "paypal_email": "x@y.z", "note": "n" * 900},
    )
    doc = build_doc(ev, "platform", 1, None)
    text = json.dumps(doc)
    assert "SIG" not in text and "x@y.z" not in text and '"K"' not in text
    assert doc["payload"]["nested"] == {"ok": 1}
    assert len(doc["detail"]) <= 500
    assert scrub({"authorization": "Bearer t"}) == {}


def test_sink_from_env():
    assert sink_from_env("platform", {}) is None
    assert sink_from_env("platform", {"ELASTICSEARCH_URL": "http://es.example.com", "ELASTIC_API_KEY": "k"}) is None
    es = Es()
    sink = sink_from_env(
        "arena", {"ELASTICSEARCH_URL": "http://localhost:9200", "ELASTIC_API_KEY": "k"}, http=es.client
    )
    assert sink is not None and sink.config.url == "http://localhost:9200" and sink.source == "arena"
    sink.close()


# ------------------------------------------------------------------ local reputation


def play_honest(w: World, deal_id: str) -> None:
    assert w.open(w.contract(deal_id)).status_code == 201
    assert w.deliver(deal_id).json()["state"] == "DELIVERED"
    assert w.attest(deal_id, evidence={"sha256_check": "match"}).json()["state"] == "SETTLED"


def play_cheat(w: World, deal_id: str) -> None:
    assert w.open(w.contract(deal_id)).status_code == 201
    w.deliver(deal_id, content_hash="b" * 64)
    assert w.attest(deal_id, verdict="reject", evidence={"sha256_check": "mismatch"}).json()["state"] == "VOIDED"


def test_local_reputation_honest_vs_cheating_seller():
    w = World().setup()
    play_honest(w, "d1")
    play_honest(w, "d2")
    play_cheat(w, "d3")
    seller = w.service.reputation.reputation("seller")
    assert seller.source == "local" and seller.as_of.endswith("Z")
    assert (seller.deals_total, seller.settled, seller.voided) == (3, 2, 1)
    assert seller.disputed == 1 and seller.rulings_lost == 1
    assert seller.dispute_rate == pytest.approx(0.3333, abs=1e-3)
    assert seller.median_delivery_seconds is not None and seller.median_delivery_seconds >= 0
    assert seller.first_seen and seller.last_seen and seller.first_seen <= seller.last_seen
    buyer = w.service.reputation.reputation("buyer")
    assert buyer.deals_total == 3 and buyer.rulings_lost == 0 and buyer.median_delivery_seconds is None
    assert w.service.reputation.reputation("seller", exclude_deal="d3").deals_total == 2
    nobody = w.service.reputation.reputation("ghost")
    assert nobody.deals_total == 0 and nobody.dispute_rate == 0.0 and nobody.first_seen is None


def test_honest_seller_has_clean_record():
    w = World().setup()
    play_honest(w, "d1")
    rep = w.service.reputation.reputation("seller")
    assert (rep.disputed, rep.rulings_lost, rep.dispute_rate) == (0, 0, 0.0)


def test_reputation_route():
    w = World().setup()
    play_honest(w, "d1")
    play_cheat(w, "d2")
    r = w.http.get("/v1/agents/seller/reputation")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "seller" and body["deals_total"] == 2 and body["rulings_lost"] == 1
    assert body["source"] == "local" and body["as_of"]
    assert w.http.get("/v1/agents/ghost/reputation").status_code == 404
    assert "reputation" not in w.http.get("/v1/agents/seller").json()  # the agent record is unchanged


def test_reputation_route_reports_a_dead_source_as_502():
    w = World().setup()

    class Dead:
        def reputation(self, agent_id: str, exclude_deal: str | None = None) -> Reputation:
            raise ElasticReputationError("down")

    w.service.reputation = Dead()  # type: ignore[assignment]
    assert w.http.get("/v1/agents/seller/reputation").status_code == 502


# ------------------------------------------------------------------ elastic reputation

ESQL_RESPONSE = {
    "columns": [
        {"name": "deals_total", "type": "long"},
        {"name": "settled", "type": "long"},
        {"name": "voided", "type": "long"},
        {"name": "disputed", "type": "long"},
        {"name": "rulings_lost", "type": "long"},
        {"name": "median_delivery_s", "type": "double"},
        {"name": "first_seen", "type": "date"},
        {"name": "last_seen", "type": "date"},
    ],
    "values": [[4, 2, 2, 2, 1, 42.5, "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z"]],
}


def test_parse_esql_response():
    rep = parse_reputation("s", ESQL_RESPONSE)
    assert (rep.deals_total, rep.settled, rep.voided, rep.disputed, rep.rulings_lost) == (4, 2, 2, 2, 1)
    assert rep.dispute_rate == 0.5 and rep.median_delivery_seconds == 42.5
    assert rep.first_seen == "2026-01-01T00:00:00.000Z" and rep.source == "elastic"


def test_parse_esql_response_with_no_history():
    empty = {"columns": ESQL_RESPONSE["columns"], "values": [[0, None, None, None, None, None, None, None]]}
    rep = parse_reputation("s", empty)
    assert rep.deals_total == 0 and rep.settled == 0 and rep.dispute_rate == 0.0 and rep.first_seen is None
    assert parse_reputation("s", {"columns": [], "values": []}).deals_total == 0


def test_elastic_backend_sends_parameterized_query_and_caches():
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        assert req.headers["authorization"] == "ApiKey KEY123"
        return httpx.Response(200, json=ESQL_RESPONSE)

    backend = ElasticReputation(CFG, http=httpx.Client(transport=httpx.MockTransport(handler)))
    evil = 'x" OR true //'
    assert backend.reputation(evil, exclude_deal="d9").deals_total == 4
    assert seen[0]["params"] == [{"agent": evil}, {"exclude_deal": "d9"}]
    query = seen[0]["query"]
    assert evil not in query and "?agent" in query and "deal_id != ?exclude_deal" in query
    backend.reputation(evil, exclude_deal="d9")
    assert len(seen) == 1  # cached
    backend.reputation("other")
    assert len(seen) == 2 and "exclude_deal" not in seen[1]["query"]


@pytest.mark.parametrize("response", [httpx.Response(401), httpx.Response(200, text="not json")])
def test_elastic_backend_errors_are_typed(response: httpx.Response):
    backend = ElasticReputation(CFG, http=httpx.Client(transport=httpx.MockTransport(lambda _r: response)))
    with pytest.raises(ElasticReputationError):
        backend.reputation("s")


def test_backend_choice_from_env():
    assert reputation_from_env(BASE) is None  # local is the default
    assert reputation_from_env({**BASE, "TRUSTEE_REPUTATION_BACKEND": "elastic"}) is not None
    assert reputation_from_env({"TRUSTEE_REPUTATION_BACKEND": "elastic"}) is None  # not configured: stay local


def test_queries_reject_unsafe_index_names():
    with pytest.raises(ValueError):
        queries.reputation_query("x | DROP")
    assert queries.reputation_query("trustee-ledger").startswith("FROM trustee-ledger")
