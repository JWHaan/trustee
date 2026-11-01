"""Northwind or Tidewater as a hosted container (Astropods): configuration from the environment, nothing else.

    python -m trustee.network.hosted northwind
    python -m trustee.network.hosted tidewater

Everything is opt-in through environment variables; the in-process agents of the arena do not use this module.

    HOST / PORT               bind address (default 0.0.0.0 and 80, the port Astropods routes to; PORT overrides)
    AGENT_PUBLIC_URL          where peers and Trustee reach this agent; goes into its Agent Card (required)
    TRUSTEE_URL               Trustee's public base URL (required)
    TRUSTEE_OPERATOR_TOKEN    Trustee's operator token, when Trustee has one
    AGENT_IDENTITY_SECRET     one secret the agent's Ed25519 keys and id derive from, stable across restarts
                              (unset: fresh keys at each start, as locally). Make one with scripts/gen_agent_secret.py
    TRUSTEE_MODE              auto | live | scripted (default auto: live when an LLM key or the AI Gateway exists)
    TIDEWATER_CHEAT           1 to ship the corrupt twin of the dataset (Tidewater only)
    TIDEWATER_URL             the seller Northwind may buy from (Northwind only; the trigger's allow-list)
    NORTHWIND_TRIGGER_SECRET  turns on Northwind's operator purchase routes (see trigger.py); unset: they 404
    NORTHWIND_ZAPIER_MCP_TOKEN, NORTHWIND_ZAPIER_TOOLS, TIDEWATER_ZAPIER_MCP_TOKEN, TIDEWATER_ZAPIER_TOOLS
                              book settled deals in each business's own apps through Zapier MCP; unset: off
                              (see docs/ZAPIER_MCP.md)

The A2A routes carry no Astropods sign-in: the agents authenticate every message with Ed25519 signatures.
`/healthz` answers 200 without touching anything.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi.responses import JSONResponse

from ..paypal import sandbox_emails
from .app import serve_forever
from .common import AgentIdentity, NetworkAgent, default_llm
from .northwind import Northwind, create_northwind
from .tidewater import Tidewater, create_tidewater
from .trigger import PurchaseRuns
from .trigger import install as install_trigger

log = logging.getLogger(__name__)

TRUE = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    """The environment is missing something the hosted agent cannot run without."""


@dataclass(frozen=True)
class HostedConfig:
    host: str
    port: int
    public_url: str
    trustee_url: str
    operator_token: str | None
    identity_secret: str | None
    mode: str
    cheat: bool
    tidewater_urls: tuple[str, ...]
    trigger_secret: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HostedConfig:
        e = os.environ if env is None else env

        def get(name: str) -> str:
            return (e.get(name) or "").strip()

        public = get("AGENT_PUBLIC_URL").rstrip("/")
        trustee = get("TRUSTEE_URL").rstrip("/")
        for name, url in (("AGENT_PUBLIC_URL", public), ("TRUSTEE_URL", trustee)):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigError(f"{name} must be an http(s) URL")
        mode = get("TRUSTEE_MODE") or "auto"
        if mode not in ("auto", "live", "scripted"):
            raise ConfigError("TRUSTEE_MODE must be auto, live or scripted")
        try:
            port = int(get("PORT") or 80)
        except ValueError:
            raise ConfigError("PORT must be a number") from None
        return cls(
            host=get("HOST") or "0.0.0.0",  # noqa: S104 - a container must listen on all interfaces
            port=port,
            public_url=public,
            trustee_url=trustee,
            operator_token=get("TRUSTEE_OPERATOR_TOKEN") or None,
            identity_secret=get("AGENT_IDENTITY_SECRET") or None,
            mode=mode,
            cheat=get("TIDEWATER_CHEAT").lower() in TRUE,
            tidewater_urls=tuple(u.strip().rstrip("/") for u in get("TIDEWATER_URL").split(",") if u.strip()),
            trigger_secret=get("NORTHWIND_TRIGGER_SECRET") or None,
        )


def _identity(cfg: HostedConfig, agent_base: str, principal_base: str, role: str, email: str) -> AgentIdentity | None:
    if cfg.identity_secret is None:
        return None
    try:
        return AgentIdentity.from_secret(cfg.identity_secret, agent_base, principal_base, role, email)
    except ValueError as exc:
        raise ConfigError(f"AGENT_IDENTITY_SECRET: {exc}") from None


def _healthz(agent: NetworkAgent) -> None:
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    agent.app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)


def build_northwind(cfg: HostedConfig) -> Northwind:
    runs = PurchaseRuns()
    overrides: dict[str, object] = {
        "trustee_url": cfg.trustee_url,
        "public_url": cfg.public_url,
        "operator_token": cfg.operator_token,
        "llm": default_llm(cfg.mode),
        "observer": runs.observe,
        "on_approval": runs.approval,
    }
    identity = _identity(cfg, "northwind-planner", "northwind-outfitters", "buyer", sandbox_emails()[0])
    if identity is not None:
        overrides["identity"] = identity
    agent = create_northwind(**overrides)
    _healthz(agent)
    if cfg.trigger_secret:
        install_trigger(agent, runs, cfg.trigger_secret, list(cfg.tidewater_urls))
    return agent


def build_tidewater(cfg: HostedConfig) -> Tidewater:
    overrides: dict[str, object] = {
        "trustee_url": cfg.trustee_url,
        "public_url": cfg.public_url,
        "operator_token": cfg.operator_token,
        "llm": default_llm(cfg.mode),
        "cheat": cfg.cheat,
    }
    identity = _identity(cfg, "tidewater-sales", "tidewater-data-co", "seller", sandbox_emails()[1])
    if identity is not None:
        overrides["identity"] = identity
    agent = create_tidewater(**overrides)
    _healthz(agent)
    return agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a hosted Trustee network agent from environment variables.")
    parser.add_argument("agent", choices=["northwind", "tidewater"])
    args = parser.parse_args(argv)
    try:
        cfg = HostedConfig.from_env()
        agent = build_northwind(cfg) if args.agent == "northwind" else build_tidewater(cfg)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return 2
    stable = "stable" if cfg.identity_secret else "fresh at each start"
    print(f"{agent.card_name} {agent.identity.agent_id} ({stable} identity) on {cfg.public_url}")
    serve_forever(agent.app, cfg.host, cfg.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
