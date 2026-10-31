"""Human alerts: ledger events -> Zapier-style webhook, without ever touching the money path.

No real network: every webhook is an httpx.MockTransport and every backoff sleep is injected.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_platform_api import World

from trustee import alerts
from trustee.alerts import AlertConfigError, AlertSink, parse_kinds, sink_from_env
from trustee.arena.api import create_app
from trustee.identity import AgentKey
from trustee.ledger import Event, Ledger
from trustee.paypal import PayPalClient

URL = "https://hooks.zapier.com/hooks/catch/1/abc/"


class Hook:
    """A recording webhook endpoint."""

    def __init__(self, statuses: list[int] | None = None):
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])

        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return httpx.Response(self.statuses.pop(0) if self.statuses else 200)

        self.client = httpx.Client(transport=httpx.MockTransport(handler))

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def make_sink(hook: Hook, secret: str | None = None, **kw: Any) -> tuple[AlertSink, Ledger]:
    sink = AlertSink(URL, secret, source=kw.pop("source", "platform"), http=hook.client, sleep=lambda _s: None, **kw)
    ledger = Ledger()
    sink.attach(ledger)
    return sink, ledger


def sent(sink: AlertSink, hook: Hook) -> list[dict[str, Any]]:
    assert sink.flush(5)
    return hook.bodies()


# ---------------------------------------------------------------- mapping


def test_each_mapped_event_makes_exactly_one_alert_with_the_right_fields():
    hook = Hook()
    sink, ledger = make_sink(hook, public_url="https://trustee.example/")
    ledger.append("d1", "trustee", "state", event="transition", state_from="DELIVERED", state_to="DISPUTED", note="x")
    ledger.append(
        "d1", "trustee", "ruling", decision="uphold", clause="c", note="n", dispute_deadline="2026-11-01T00:00:00"
    )
    ledger.append("d2", "trustee", "paypal_alert", event="PAYMENT.CAPTURE.REVERSED", detail="d", state="SETTLED")
    ledger.append("d3", "trustee", "identity", event="verify_delivery", signer="seller", ok=False, note="bad")
    ledger.append("d4", "trustee", "mandate", report="r", ok=False, when="approval")
    ledger.append("d5", "trustee", "paypal", op="capture", error="boom", status_code=500, request_id="r")
    ledger.append(
        "d6", "trustee", "ruling", decision="settle", override=True, operator="ops", clause="operator override"
    )
    ledger.append("d7", "trustee", "note", op="unwind", explanation="deadline lapsed before funds were held")
    got = {a["alert"]: a for a in sent(sink, hook)}
    assert set(got) == {
        "dispute",
        "paypal_alert",
        "signature_rejected",
        "mandate_recheck",
        "paypal_call_failed",
        "operator_resolved",
        "approval_expired",
    }
    d = got["dispute"]
    assert d["title"] == "Dispute opened — funds held"
    assert d["severity"] == "warning"
    assert d["deal_id"] == "d1" and d["state"] == "DISPUTED" and d["source"] == "platform"
    assert d["dispute_deadline"] == "2026-11-01T00:00:00"
    assert "2026-11-01" in d["summary"]
    assert d["deal_url"] == "https://trustee.example/v1/deals/d1"
    assert d["arena_url"] == "https://trustee.example"
    assert d["occurred_at"]
    assert got["paypal_alert"]["severity"] == "critical"
    assert got["signature_rejected"]["title"] == "Signature rejected"
    assert got["mandate_recheck"]["title"] == "Mandate failed re-check"
    assert got["paypal_call_failed"]["title"] == "PayPal call failed — funds still held, retry pending"
    assert got["operator_resolved"]["title"] == "Operator resolved a dispute"
    assert got["approval_expired"]["title"] == "Payer approval expired"
    for a in got.values():
        assert set(a) >= {"alert", "title", "summary", "severity", "deal_id", "state", "actor", "source", "occurred_at"}
        assert a["severity"] in ("info", "warning", "critical")
    sink.close()


def test_capture_time_mandate_hold_is_a_mandate_alert():
    hook = Hook()
    sink, ledger = make_sink(hook)
    ledger.append("d1", "trustee", "ruling", decision="hold", clause="mandate recheck", note="n", objective=True)
    assert [a["alert"] for a in sent(sink, hook)] == ["mandate_recheck"]
    sink.close()


def test_arena_approval_timeout_and_cancel_are_alerts_on_the_arena_source():
    hook = Hook()
    sink, ledger = make_sink(hook, source="arena")
    ledger.append("a1", "arena", "run", event="approval_timeout", note="no approval before the timeout")
    ledger.append("a2", "arena", "run", event="approval_cancelled", note="buyer cancelled at PayPal")
    got = sent(sink, hook)
    assert [a["alert"] for a in got] == ["approval_expired", "approval_expired"]
    assert {a["source"] for a in got} == {"arena"}
    assert {a["deal_url"] for a in got} == {""}  # deal_url is a platform concept
    sink.close()


def test_permanent_authorize_refusal_is_an_approval_failed_alert():
    hook = Hook()
    sink, ledger = make_sink(hook)
    ledger.append("d1", "trustee", "paypal", op="authorize", error="422", status_code=422, request_id="r")
    ledger.append("d2", "trustee", "paypal", op="authorize", error="503", status_code=503, request_id="r")
    ledger.append("d3", "trustee", "paypal", op="authorize", error="timeout", status_code=None, request_id="r")
    got = sent(sink, hook)
    assert [(a["alert"], a["deal_id"]) for a in got] == [("approval_failed", "d1")]
    assert "PAYPAL_SELLER_EMAIL" in got[0]["summary"]
    sink.close()

    hook = Hook()
    sink, ledger = make_sink(hook, source="arena")
    ledger.append("a1", "arena", "run", event="approval_failed", note="HTTP 422")
    assert [a["alert"] for a in sent(sink, hook)] == ["approval_failed"]
    sink.close()


def test_unrelated_events_make_no_alert():
    hook = Hook()
    sink, ledger = make_sink(hook)
    ledger.append("d1", "trustee", "msg", event="contract_formed")
    ledger.append("d1", "trustee", "mandate", report="ok", ok=True)
    ledger.append("d1", "trustee", "identity", event="verify_delivery", signer="seller", ok=True)
    ledger.append("d1", "trustee", "ruling", decision="release", clause="c")
    ledger.append("d1", "trustee", "paypal", op="capture", order_id="o", note="settled to seller")
    ledger.append("d1", "trustee", "paypal", op="authorize", error="boom")  # not a capture/void failure
    ledger.append("d1", "arena", "run", event="run_finished")
    ledger.append("d1", "trustee", "note", op="unwind", explanation="voided; buyer refunded — clause: evidence clause")
    ledger.append("d1", "trustee", "note", op="approval", explanation="awaiting buyer approval in PayPal")
    ledger.append("d1", "trustee", "state", event="transition", state_from="DELIVERED", state_to="DISPUTED")
    assert sent(sink, hook) == []
    sink.close()


def test_dedupe_on_deal_and_alert_type():
    hook = Hook()
    sink, ledger = make_sink(hook)
    for _ in range(3):
        ledger.append("d1", "trustee", "mandate", report="r", ok=False, when="approval")
    ledger.append("d2", "trustee", "mandate", report="r", ok=False, when="approval")
    assert [a["deal_id"] for a in sent(sink, hook)] == ["d1", "d2"]
    sink.close()


def test_kinds_filter_and_parse():
    hook = Hook()
    sink, ledger = make_sink(hook, kinds=parse_kinds("dispute, bogus"))
    ledger.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    ledger.append("d1", "trustee", "ruling", decision="uphold", clause="c")
    assert [a["alert"] for a in sent(sink, hook)] == ["dispute"]
    assert parse_kinds(None) == alerts.DEFAULT_KINDS
    assert parse_kinds("bogus") == alerts.DEFAULT_KINDS
    sink.close()


def test_payload_never_carries_secrets_or_raw_notes():
    hook = Hook()
    sink, ledger = make_sink(hook)
    ledger.append(
        "d1", "trustee", "identity", event="v", signer="s", ok=False, note="sig=SECRETSIG key=SECRETKEY a@b.com"
    )
    ledger.append("d2", "trustee", "paypal_alert", event="e", detail="payer@example.com raw body {...}")
    text = json.dumps(sent(sink, hook))
    for needle in ("SECRETSIG", "SECRETKEY", "a@b.com", "payer@example.com", "raw body"):
        assert needle not in text
    sink.close()


# --------------------------------------------------------------- delivery


def test_hmac_headers_are_correct_and_absent_without_a_secret():
    hook = Hook()
    sink, ledger = make_sink(hook, secret="topsecret")
    ledger.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    sent(sink, hook)
    req = hook.requests[0]
    expect = hmac.new(b"topsecret", req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-Trustee-Signature"] == f"sha256={expect}"
    assert int(req.headers["X-Trustee-Timestamp"]) > 0
    assert req.headers["content-type"] == "application/json"
    sink.close()

    plain = Hook()
    sink2, ledger2 = make_sink(plain)
    ledger2.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    sent(sink2, plain)
    assert "X-Trustee-Signature" not in plain.requests[0].headers
    sink2.close()


def test_retries_with_backoff_then_succeeds():
    hook = Hook([500, 503, 200])
    naps: list[float] = []
    sink = AlertSink(URL, source="platform", http=hook.client, sleep=naps.append)
    ledger = Ledger()
    sink.attach(ledger)
    ledger.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    assert sink.flush(5)
    assert len(hook.requests) == 3
    assert naps == [1.0, 2.0]
    sink.close()


def test_gives_up_after_retries_without_raising(caplog: pytest.LogCaptureFixture):
    hook = Hook([500] * 10)
    sink, ledger = make_sink(hook, retries=2)
    ledger.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    assert sink.flush(5)
    assert len(hook.requests) == 3
    assert "giving up" in caplog.text
    # The sink is still alive for the next alert.
    ledger.append("d2", "trustee", "paypal_alert", event="e", detail="d")
    assert sink.flush(5)
    assert len(hook.requests) == 6
    sink.close()


def test_client_error_is_not_retried_and_transport_errors_are_swallowed():
    hook = Hook([400])
    sink, ledger = make_sink(hook)
    ledger.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    assert sink.flush(5)
    assert len(hook.requests) == 1
    sink.close()

    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    sink2 = AlertSink(
        URL, source="platform", http=httpx.Client(transport=httpx.MockTransport(boom)), sleep=lambda _s: None
    )
    ledger2 = Ledger()
    sink2.attach(ledger2)
    ledger2.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    assert sink2.flush(5)
    sink2.close()


def test_a_hanging_endpoint_never_delays_ledger_append():
    release = threading.Event()
    started = threading.Event()

    def hang(_req: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(10)
        return httpx.Response(200)

    sink = AlertSink(URL, source="platform", http=httpx.Client(transport=httpx.MockTransport(hang)))
    ledger = Ledger()
    sink.attach(ledger)
    t0 = time.monotonic()
    for i in range(20):
        ledger.append(f"d{i}", "trustee", "paypal_alert", event="e", detail="d")
    assert time.monotonic() - t0 < 0.5
    assert started.wait(2)
    release.set()
    assert sink.flush(5)
    sink.close()


def test_queue_overflow_drops_the_oldest(caplog: pytest.LogCaptureFixture):
    release = threading.Event()
    started = threading.Event()
    seen: list[str] = []

    def gate(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content)["deal_id"])
        started.set()
        release.wait(10)
        return httpx.Response(200)

    sink = AlertSink(URL, source="platform", http=httpx.Client(transport=httpx.MockTransport(gate)), max_queue=2)
    ledger = Ledger()
    sink.attach(ledger)
    ledger.append("d0", "trustee", "paypal_alert", event="e", detail="d")
    assert started.wait(2)  # d0 is in flight, the queue is empty
    for i in (1, 2, 3, 4):
        ledger.append(f"d{i}", "trustee", "paypal_alert", event="e", detail="d")
    release.set()
    assert sink.flush(5)
    assert seen == ["d0", "d3", "d4"]
    assert "dropped oldest" in caplog.text
    sink.close()


def test_close_unsubscribes():
    hook = Hook()
    sink, ledger = make_sink(hook)
    sink.close()
    ledger.append("d1", "trustee", "paypal_alert", event="e", detail="d")
    time.sleep(0.05)
    assert hook.requests == []


# ------------------------------------------------------------ configuration


@pytest.mark.parametrize("bad", ["http://hooks.zapier.com/x", "ftp://x/y", "not a url", "https://"])
def test_non_https_url_is_refused(bad: str):
    with pytest.raises(AlertConfigError):
        AlertSink(bad, source="platform")


@pytest.mark.parametrize("ok", ["http://127.0.0.1:9/x", "http://localhost:9/x", URL])
def test_https_and_loopback_urls_are_accepted(ok: str):
    AlertSink(ok, source="platform", http=Hook().client).close()


def test_bad_env_url_runs_without_alerts_and_logs(caplog: pytest.LogCaptureFixture):
    assert sink_from_env("platform", env={"TRUSTEE_ALERT_WEBHOOK_URL": "http://evil.example/x"}) is None
    assert "alerts disabled" in caplog.text
    assert sink_from_env("platform", env={}) is None


def test_cli_test_sends_one_sample(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    hook = Hook()
    real = httpx.Client
    monkeypatch.setattr(
        alerts.httpx, "Client", lambda *a, **k: hook.client if not a and "transport" not in k else real(*a, **k)
    )
    monkeypatch.setenv("TRUSTEE_ALERT_WEBHOOK_URL", URL)
    monkeypatch.setenv("TRUSTEE_ALERT_WEBHOOK_SECRET", "s")
    assert alerts.main(["--test"]) == 0
    assert len(hook.requests) == 1
    assert hook.bodies()[0]["alert"] == "dispute"
    assert "X-Trustee-Signature" in hook.requests[0].headers
    out = capsys.readouterr().out
    assert "delivered" in out


def test_cli_test_fails_non_zero_without_url_or_on_rejection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRUSTEE_ALERT_WEBHOOK_URL", raising=False)
    assert alerts.main(["--test"]) == 1
    hook = Hook([400])
    monkeypatch.setattr(alerts.httpx, "Client", lambda *a, **k: hook.client)
    monkeypatch.setenv("TRUSTEE_ALERT_WEBHOOK_URL", URL)
    assert alerts.main(["--test"]) == 1


# ----------------------------------------------------------------- app wiring


def _alert_subs(ledger: Ledger) -> list[Any]:
    return [cb for cb in ledger._subscribers if isinstance(getattr(cb, "__self__", None), AlertSink)]


def _app(monkeypatch: pytest.MonkeyPatch, hook: Hook | None, arena: str | None = None, ledger: Ledger | None = None):
    for key in (
        "TRUSTEE_ALERT_WEBHOOK_URL",
        "TRUSTEE_ALERT_WEBHOOK_SECRET",
        "TRUSTEE_ALERT_KINDS",
        "TRUSTEE_ALERT_ARENA",
    ):
        monkeypatch.delenv(key, raising=False)
    if hook is not None:
        monkeypatch.setenv("TRUSTEE_ALERT_WEBHOOK_URL", URL)
        real = httpx.Client
        monkeypatch.setattr(
            alerts.httpx, "Client", lambda *a, **k: hook.client if not a and "transport" not in k else real(*a, **k)
        )
    if arena is not None:
        monkeypatch.setenv("TRUSTEE_ALERT_ARENA", arena)
    return create_app(ledger=ledger or Ledger(), paypal=PayPalClient(dry_run=True))


def test_env_unset_means_healthz_off_and_no_subscriber(monkeypatch: pytest.MonkeyPatch):
    app = _app(monkeypatch, None)
    assert TestClient(app).get("/healthz").json()["alerts"] == "off"
    assert app.state.alert_sinks == []
    assert _alert_subs(app.state.service.ledger) == []
    assert _alert_subs(app.state.ledger) == []


def test_env_set_attaches_to_platform_only_by_default(monkeypatch: pytest.MonkeyPatch):
    app = _app(monkeypatch, Hook())
    assert TestClient(app).get("/healthz").json()["alerts"] == "webhook"
    assert len(_alert_subs(app.state.service.ledger)) == 1
    assert _alert_subs(app.state.ledger) == []  # arena ledger stays silent
    for sink in app.state.alert_sinks:
        sink.close()


def test_arena_flag_attaches_to_the_arena_ledger_too(monkeypatch: pytest.MonkeyPatch):
    app = _app(monkeypatch, Hook(), arena="1")
    assert [s.source for s in app.state.alert_sinks] == ["platform", "arena"]
    assert len(_alert_subs(app.state.ledger)) == 1
    for sink in app.state.alert_sinks:
        sink.close()


def test_lifespan_closes_the_sinks(monkeypatch: pytest.MonkeyPatch):
    app = _app(monkeypatch, Hook())
    sink = app.state.alert_sinks[0]
    with TestClient(app):
        assert sink._thread.is_alive()
    assert not sink._thread.is_alive()
    assert _alert_subs(app.state.service.ledger) == []


# ------------------------------------------------------------------ end to end


def test_forged_contract_over_v1_sends_signature_rejected():
    hook = Hook()
    w = World().setup()
    sink = AlertSink(URL, source="platform", http=hook.client, sleep=lambda _s: None)
    sink.attach(w.service.ledger)
    c = w.contract(sign=False).sign("buyer", AgentKey.generate()).sign("seller", w.keys["seller"])
    assert w.open(c).status_code == 401
    got = sent(sink, hook)
    assert [a["alert"] for a in got] == ["signature_rejected"]
    assert got[0]["deal_id"] == "deal-1"
    sink.close()


def test_cheat_run_through_the_arena_alerts_only_when_arena_flag_is_on(monkeypatch: pytest.MonkeyPatch):
    async def run(app: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arena") as client:
            created = (await client.post("/api/runs", json={"scenario": "cheat", "mode": "scripted"})).json()
            for _ in range(750):
                snap = (await client.get(f"/api/runs/{created['run_id']}")).json()
                if snap["state"] != "RUNNING":
                    return snap
                await asyncio.sleep(0.02)
        raise AssertionError("run did not finish")

    quiet = Hook()
    app_off = _app(monkeypatch, quiet, arena="0")
    assert asyncio.run(run(app_off))["state"] == "VOIDED"
    for sink in app_off.state.alert_sinks:
        assert sink.flush(5)
        sink.close()
    assert quiet.requests == []

    loud = Hook()
    app_on = _app(monkeypatch, loud, arena="1")
    assert asyncio.run(run(app_on))["state"] == "VOIDED"
    for sink in app_on.state.alert_sinks:
        assert sink.flush(5)
    got = loud.bodies()
    # The cheat is an objective breach: the arbiter releases the funds (a refund), so no dispute opens and
    # no human decision is needed. Nothing may page anyone for a correctly refunded cheat.
    assert got == []
    for sink in app_on.state.alert_sinks:
        sink.close()


def test_forged_arena_scenario_sends_signature_rejected_when_arena_flag_is_on(monkeypatch: pytest.MonkeyPatch):
    hook = Hook()
    app = _app(monkeypatch, hook, arena="1")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arena") as client:
            created = (await client.post("/api/runs", json={"scenario": "forged", "mode": "scripted"})).json()
            for _ in range(750):
                snap = (await client.get(f"/api/runs/{created['run_id']}")).json()
                if snap["state"] != "RUNNING":
                    return
                await asyncio.sleep(0.02)

    asyncio.run(run())
    for sink in app.state.alert_sinks:
        assert sink.flush(5)
    got = hook.bodies()
    assert [(a["alert"], a["source"]) for a in got] == [("signature_rejected", "arena")]
    for sink in app.state.alert_sinks:
        sink.close()


def test_ledger_event_dataclass_is_what_the_sink_consumes():
    # Guard against a Ledger refactor silently changing the fields classify() reads.
    ev = Event(ts="2026-10-31T07:00:00.000", deal_id="d", actor="trustee", kind="paypal_alert", payload={})
    assert alerts.classify(ev) is not None
