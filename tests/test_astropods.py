"""Hosting Northwind and Tidewater on Astropods: AI Gateway fallback, env-driven agent config, a stable identity,
the operator purchase trigger, and the arena's remote mode against agents served on real loopback ports.

No outside network: the "remote" agents and the arena all listen on 127.0.0.1, and PayPal is the dry-run client.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trustee.arena.api import create_app
from trustee.arena.rows import to_row
from trustee.arena.scenario import get_scenario
from trustee.ledger import Ledger
from trustee.llm import LLM, gateway_from_env
from trustee.network.app import free_port, start_server
from trustee.network.common import AgentIdentity, default_llm
from trustee.network.hosted import ConfigError, HostedConfig, build_northwind, build_tidewater
from trustee.network.remote import RemoteNetwork
from trustee.network.trigger import authorized
from trustee.paypal import PayPalClient

ENV_NAMES = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "ASTRO_GATEWAY_URL",
    "ASTRO_GATEWAY_API_KEY",
    "MODEL_DEFAULT",
)
NIM = "https://integrate.api.nvidia.com/v1"
SECRET = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
OTHER_SECRET = base64.urlsafe_b64encode(bytes(range(1, 33))).decode().rstrip("=")
TRIGGER = "trigger-secret-for-tests"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*ENV_NAMES, "NORTHWIND_URL", "TIDEWATER_URL", "NORTHWIND_TRIGGER_SECRET"):
        monkeypatch.delenv(name, raising=False)


def seen(monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[str, str, str]:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    llm = LLM()
    return str(llm.client.base_url).rstrip("/"), llm.client.api_key, llm.model


# ----------------------------------------------------------------------------------- llm precedence


def test_no_variables_means_todays_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    assert seen(monkeypatch)[0] == NIM
    assert seen(monkeypatch)[1] == "no-key"
    assert gateway_from_env() is None


def test_the_gateway_is_used_when_no_llm_variable_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    got = seen(
        monkeypatch, ASTRO_GATEWAY_URL="https://gw.example/", ASTRO_GATEWAY_API_KEY="gw-key", MODEL_DEFAULT="m-1"
    )
    assert got == ("https://gw.example/v1", "gw-key", "m-1")


def test_llm_model_beats_model_default_but_the_gateway_still_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    got = seen(
        monkeypatch,
        ASTRO_GATEWAY_URL="https://gw.example",
        ASTRO_GATEWAY_API_KEY="gw-key",
        MODEL_DEFAULT="m-1",
        LLM_MODEL="mine",
    )
    assert got == ("https://gw.example/v1", "gw-key", "mine")


@pytest.mark.parametrize("explicit", [{"LLM_BASE_URL": "https://mine.example/v1"}, {"LLM_API_KEY": "my-key"}])
def test_any_explicit_llm_setting_switches_the_gateway_off(monkeypatch: pytest.MonkeyPatch, explicit: dict) -> None:
    base, key, model = seen(
        monkeypatch,
        ASTRO_GATEWAY_URL="https://gw.example",
        ASTRO_GATEWAY_API_KEY="gw-key",
        MODEL_DEFAULT="m-1",
        **explicit,
    )
    assert key != "gw-key" and "gw.example" not in base
    assert model != "m-1"  # the gateway's model name means nothing to another endpoint
    assert gateway_from_env() is None


def test_explicit_llm_variables_alone_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    assert seen(monkeypatch, LLM_BASE_URL="https://a/v1", LLM_API_KEY="k", LLM_MODEL="x") == ("https://a/v1", "k", "x")


def test_a_half_configured_gateway_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_GATEWAY_URL", "https://gw.example")
    assert gateway_from_env() is None
    assert seen(monkeypatch)[0] == NIM


def test_constructor_arguments_still_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRO_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("ASTRO_GATEWAY_API_KEY", "gw-key")
    llm = LLM(base_url="https://x/v1", api_key="k", model="m")
    assert (str(llm.client.base_url).rstrip("/"), llm.client.api_key, llm.model) == ("https://x/v1", "k", "m")


def test_the_gateway_key_makes_auto_mode_live(monkeypatch: pytest.MonkeyPatch) -> None:
    from trustee.arena.scripted import ScriptedLLM

    assert isinstance(default_llm("auto"), ScriptedLLM)
    monkeypatch.setenv("ASTRO_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("ASTRO_GATEWAY_API_KEY", "gw-key")
    assert isinstance(default_llm("auto"), LLM)


# ---------------------------------------------------------------------------------------- identity


def make_identity(secret: str) -> AgentIdentity:
    return AgentIdentity.from_secret(secret, "northwind-planner", "northwind-outfitters", "buyer", "b@example.com")


def test_an_identity_from_a_secret_is_the_same_at_every_start() -> None:
    a, b = make_identity(SECRET), make_identity(SECRET)
    assert a.agent_id == b.agent_id and a.principal == b.principal
    assert a.public_key == b.public_key and a.principal_key.public_key_b64 == b.principal_key.public_key_b64
    assert a.public_key != a.principal_key.public_key_b64
    assert make_identity(OTHER_SECRET).agent_id != a.agent_id


@pytest.mark.parametrize("bad", ["", "not base64 !!", base64.urlsafe_b64encode(b"short").decode()])
def test_a_bad_identity_secret_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        make_identity(bad)


def test_without_a_secret_the_identity_is_fresh_as_before() -> None:
    cfg = HostedConfig.from_env({"AGENT_PUBLIC_URL": "https://a.example", "TRUSTEE_URL": "https://t.example"})
    one, two = build_northwind(cfg), build_northwind(cfg)
    assert one.identity.agent_id != two.identity.agent_id


# ------------------------------------------------------------------------------------ hosted config

BASE_ENV = {
    "AGENT_PUBLIC_URL": "https://nw.example/",
    "TRUSTEE_URL": "https://trustee.example/",
    "TRUSTEE_MODE": "scripted",
}


def test_config_reads_the_environment() -> None:
    cfg = HostedConfig.from_env(
        {
            **BASE_ENV,
            "PORT": "8080",
            "TRUSTEE_OPERATOR_TOKEN": "op",
            "TIDEWATER_CHEAT": "1",
            "TIDEWATER_URL": "https://tw.example/, https://tw2.example",
            "AGENT_IDENTITY_SECRET": SECRET,
        }
    )
    assert (cfg.port, cfg.host) == (8080, "0.0.0.0")
    assert cfg.public_url == "https://nw.example" and cfg.trustee_url == "https://trustee.example"
    assert cfg.operator_token == "op" and cfg.cheat and cfg.identity_secret == SECRET
    assert cfg.tidewater_urls == ("https://tw.example", "https://tw2.example")
    assert HostedConfig.from_env(BASE_ENV).port == 80 and not HostedConfig.from_env(BASE_ENV).cheat


@pytest.mark.parametrize(
    "env",
    [
        {"TRUSTEE_URL": "https://t.example"},
        {"AGENT_PUBLIC_URL": "https://a.example"},
        {**BASE_ENV, "AGENT_PUBLIC_URL": "ftp://x"},
        {**BASE_ENV, "TRUSTEE_MODE": "bogus"},
        {**BASE_ENV, "PORT": "eighty"},
    ],
)
def test_config_refuses_what_it_cannot_run_without(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        HostedConfig.from_env(env)


def test_a_bad_identity_secret_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        build_northwind(HostedConfig.from_env({**BASE_ENV, "AGENT_IDENTITY_SECRET": "x"}))


def test_the_agents_use_their_config_and_keep_their_identity_across_restarts() -> None:
    cfg = HostedConfig.from_env({**BASE_ENV, "AGENT_IDENTITY_SECRET": SECRET, "TIDEWATER_CHEAT": "true"})
    nw, tw = build_northwind(cfg), build_tidewater(cfg)
    assert nw.trustee_url == "https://trustee.example" and nw.public_url == "https://nw.example"
    assert tw.cheat is True and build_tidewater(HostedConfig.from_env(BASE_ENV)).cheat is False
    again = build_northwind(cfg)
    assert again.identity.agent_id == nw.identity.agent_id and again.identity.public_key == nw.identity.public_key
    card = TestClient(nw.app).get("/.well-known/agent-card.json").json()
    assert "https://nw.example/a2a" in str(card)
    assert nw.identity.public_key in str(card)
    assert tw.identity.agent_id.startswith("tidewater-sales-")


def test_healthz_needs_no_auth() -> None:
    cfg = HostedConfig.from_env({**BASE_ENV, "NORTHWIND_TRIGGER_SECRET": TRIGGER})
    for agent in (build_northwind(cfg), build_tidewater(cfg)):
        resp = TestClient(agent.app).get("/healthz")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------------------- the trigger


def trigger_client(secret: str | None, sellers: str = "https://tw.example") -> TestClient:
    env = {**BASE_ENV, "TIDEWATER_URL": sellers}
    if secret is not None:
        env["NORTHWIND_TRIGGER_SECRET"] = secret
    return TestClient(build_northwind(HostedConfig.from_env(env)).app)


def test_the_trigger_is_off_without_a_secret() -> None:
    client = trigger_client(None)
    body = {"deal_id": "d1", "seller_url": "https://tw.example"}
    assert client.post("/operator/purchases", json=body).status_code == 404
    assert client.post("/operator/purchases", json=body, headers={"Authorization": "Bearer "}).status_code == 404
    assert client.get("/operator/purchases/d1").status_code == 404


def test_the_trigger_wants_the_right_secret() -> None:
    client = trigger_client(TRIGGER)
    body = {"deal_id": "d1", "seller_url": "https://tw.example"}
    assert client.post("/operator/purchases", json=body).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/operator/purchases", json=body, headers=wrong).status_code == 401
    assert client.get("/operator/purchases/d1", headers=wrong).status_code == 401
    assert client.get("/operator/purchases/d1").status_code == 401
    right = {"Authorization": f"Bearer {TRIGGER}"}
    assert client.get("/operator/purchases/d1", headers=right).status_code == 404  # authorized, nothing there


def test_authorized_compares_the_whole_secret() -> None:
    assert authorized("Bearer s3", "s3") and authorized("bearer s3", "s3")
    assert not authorized(None, "s3") and not authorized("Bearer s", "s3") and not authorized("Bearer s33", "s3")
    assert not authorized("s3", "s3") and not authorized("Basic s3", "s3")


def test_the_trigger_only_buys_from_the_configured_seller() -> None:
    client = trigger_client(TRIGGER)
    right = {"Authorization": f"Bearer {TRIGGER}"}
    other = client.post(
        "/operator/purchases", json={"deal_id": "d1", "seller_url": "https://evil.example"}, headers=right
    )
    assert other.status_code == 403
    missing = client.post("/operator/purchases", json={"seller_url": "https://tw.example"}, headers=right)
    assert missing.status_code == 422
    assert client.post("/operator/purchases", content=b"[]", headers=right).status_code == 422


def test_a_second_purchase_waits_for_the_first() -> None:
    from trustee.network.trigger import PurchaseRuns, install

    nw = build_northwind(HostedConfig.from_env(BASE_ENV))  # no secret: no routes of its own
    install(nw, PurchaseRuns(), "s3", ["https://tw.example"], spawn=lambda fn: None)  # a purchase that never ends
    client = TestClient(nw.app)
    mine = {"Authorization": "Bearer s3"}
    body = {"deal_id": "d1", "seller_url": "https://tw.example"}
    assert client.post("/operator/purchases", json=body, headers=mine).status_code == 202
    assert client.post("/operator/purchases", json={**body, "deal_id": "d2"}, headers=mine).status_code == 409
    assert client.get("/operator/purchases/d1", headers=mine).json()["state"] == "running"


# -------------------------------------------------------------------------------- remote arena mode


def test_remote_network_needs_all_three_settings() -> None:
    full = {
        "NORTHWIND_URL": "https://nw.example/",
        "TIDEWATER_URL": "https://tw.example",
        "NORTHWIND_TRIGGER_SECRET": "hush-123",
    }
    remote = RemoteNetwork.from_env(full)
    assert remote is not None and remote.northwind_url == "https://nw.example"
    assert "hush-123" not in repr(remote)
    for name in full:
        assert RemoteNetwork.from_env({k: v for k, v in full.items() if k != name}) is None
    assert RemoteNetwork.from_env({}) is None


class Hosted:
    """Trustee (the arena app) and two hosted agents, each on its own loopback port."""

    def __init__(self, stack: ExitStack, *, cheat: bool = False, secret: str = TRIGGER):
        trustee_port, nw_port, tw_port = free_port(), free_port(), free_port()
        self.trustee_url = f"http://127.0.0.1:{trustee_port}"
        self.nw_url = f"http://127.0.0.1:{nw_port}"
        self.tw_url = f"http://127.0.0.1:{tw_port}"
        self.app = create_app(
            ledger=Ledger(),
            paypal=PayPalClient(dry_run=True),
            self_client=lambda: httpx.Client(base_url=self.trustee_url),
        )
        common = {"TRUSTEE_URL": self.trustee_url, "TRUSTEE_MODE": "scripted", "AGENT_IDENTITY_SECRET": SECRET}
        tw_cfg = HostedConfig.from_env(
            {**common, "AGENT_PUBLIC_URL": self.tw_url, "TIDEWATER_CHEAT": "1" if cheat else "0"}
        )
        nw_cfg = HostedConfig.from_env(
            {
                **common,
                "AGENT_PUBLIC_URL": self.nw_url,
                "TIDEWATER_URL": self.tw_url,
                "NORTHWIND_TRIGGER_SECRET": TRIGGER,
            }
        )
        self.tidewater = build_tidewater(tw_cfg)
        self.northwind = build_northwind(nw_cfg)
        for app, port in ((self.app, trustee_port), (self.northwind.app, nw_port), (self.tidewater.app, tw_port)):
            handle = start_server(app, "127.0.0.1", port)
            stack.callback(handle.stop)
        self.env = {"NORTHWIND_URL": self.nw_url, "TIDEWATER_URL": self.tw_url, "NORTHWIND_TRIGGER_SECRET": secret}


@pytest.fixture
def stack() -> Iterator[ExitStack]:
    with ExitStack() as s:
        yield s


def run_remote(hosted: Hosted, monkeypatch: pytest.MonkeyPatch, scenario: str = "a2a") -> Any:
    for k, v in hosted.env.items():
        monkeypatch.setenv(k, v)
    runner = hosted.app.state.runner
    return runner, runner.run(get_scenario(scenario), mode="scripted")


def test_the_arena_runs_three_a2a_agents_against_hosted_agents(
    stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = Hosted(stack)
    runner, result = run_remote(hosted, monkeypatch)
    assert result.error is None and result.outcome is None
    assert result.state == "SETTLED" and result.price == "156.00"
    rows = [to_row(e, i) for i, e in enumerate(runner.ledger.tail(result.deal_id))]
    events = [r["event"] for r in rows if r["kind"] == "a2a"]
    assert "tidewater SendMessage quote" in events and "trustee SendMessage open-escrow" in events
    assert any(r["event"] == "a2a_network" and "hosted" in r["note"] for r in rows)
    assert any(r["kind"] == "paypal" and r["paypal_op"] == "capture" for r in rows)
    assert rows[-1]["event"] == "run_finished"
    # the deal was made by the hosted agents' stable identities, not the arena's in-process ones
    assert hosted.northwind.identity.agent_id.startswith("northwind-planner-")
    assert hosted.app.state.northwind.identity.agent_id != hosted.northwind.identity.agent_id
    assert hosted.app.state.northwind.trustee_url is None  # the in-process agents were never borrowed


def test_a_cheating_hosted_seller_is_refunded(stack: ExitStack, monkeypatch: pytest.MonkeyPatch) -> None:
    hosted = Hosted(stack, cheat=True)
    _runner, result = run_remote(hosted, monkeypatch)
    assert result.state == "VOIDED" and result.attest is not None and result.attest["verdict"] == "reject"


def test_a_wrong_trigger_secret_fails_the_run_without_a_purchase(
    stack: ExitStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = Hosted(stack, secret="wrong")
    runner, result = run_remote(hosted, monkeypatch)
    assert result.outcome is not None and "401" in result.outcome
    rows = [to_row(e, i) for i, e in enumerate(runner.ledger.tail(result.deal_id))]
    assert any(r["event"] == "remote_failed" for r in rows)
    assert not any(r["kind"] == "paypal" for r in rows)


def test_with_no_remote_settings_the_in_process_agents_run(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, TestClient] = {}
    app = create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True), self_client=lambda: holder["http"])
    holder["http"] = TestClient(app, base_url="http://127.0.0.1:8000")
    app.state.a2a.push.http = holder["http"]
    result = app.state.runner.run(get_scenario("a2a"), mode="scripted")
    assert result.state == "SETTLED"
