"""Export the public platform API as OpenAPI 3.1 into `openapi/trustee-v1.json`.

The spec is generated from the running app, so it cannot drift from the handlers. `tests/test_openapi.py`
re-runs this and fails when the committed file is stale.

Scope: `/v1/*`, `/.well-known/*` and the A2A JSON-RPC endpoint `/a2a` only. The arena UI routes (`/api/*`, `/`)
and `/healthz` are not part of the platform contract an outside agent integrates against, so they are left out
rather than tagged.

Usage:
    .venv/bin/python scripts/export_openapi.py            # write openapi/trustee-v1.json
    .venv/bin/python scripts/export_openapi.py --check    # exit 1 if the committed file is stale
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "openapi" / "trustee-v1.json"
PUBLIC_PREFIXES = ("/v1/", "/.well-known/", "/a2a")

SERVERS = [
    {"url": "https://trustee-arena.onrender.com", "description": "Hosted demo"},
    {"url": "http://127.0.0.1:8000", "description": "Local development (uvicorn)"},
]

TAGS = [
    {"name": "Principals", "description": "Humans or companies that own agents and vouch for them."},
    {"name": "Agents", "description": "Agent keys registered under a principal."},
    {"name": "Mandates", "description": "Signed spending or receiving limits that the deal gate enforces."},
    {"name": "Deals", "description": "Escrowed deals: open, deliver, attest, inspect, resolve."},
    {"name": "PayPal", "description": "Payer redirects and webhooks. Money moves only in these flows."},
    {"name": "Discovery", "description": "A2A Agent Card for discovering Trustee's skills."},
    {
        "name": "A2A",
        "description": "Agent2Agent JSON-RPC binding: invoke skills, follow a deal as a task, get push updates.",
    },
]

INTRO = (
    "Trustee is an escrow and arbitration layer for agent-to-agent payments on PayPal. A buyer agent's funds are "
    "authorized (held), not captured, until the buyer attests that the seller's delivery matches the contract. "
    "Accept captures the funds to the seller. Reject opens a dispute that an arbiter rules on, and a ruling "
    "either captures or voids the hold. Dry-run mode (no PayPal credentials) keeps the same API surface.\n\n"
    "## Signing scheme\n\n"
)


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def _collect_refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value.rsplit("/", 1)[-1])
            else:
                _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def _prune_components(spec: dict[str, Any]) -> None:
    """Drop component schemas no public path reaches, directly or through another schema."""
    components: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    reached: set[str] = set()
    _collect_refs(spec["paths"], reached)
    frontier = set(reached)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            if name in components:
                _collect_refs(components[name], nxt)
        frontier = nxt - reached
        reached |= nxt
    if components:
        spec["components"]["schemas"] = {k: v for k, v in components.items() if k in reached}
        if not spec["components"]["schemas"]:
            del spec["components"]["schemas"]


def build_spec() -> dict[str, Any]:
    # Imported here so `--help` and the test's import of this module stay cheap and side-effect free.
    from trustee.arena.api import create_app
    from trustee.ledger import Ledger
    from trustee.paypal import PayPalClient
    from trustee.platform import DealService
    from trustee.platform.agent_card import SIGNING_SCHEME
    from trustee.store import Store

    # An in-memory service: building the schema must not open the deployment's database or touch PayPal.
    service = DealService(Store(":memory:"), PayPalClient(dry_run=True), Ledger(), operator_token=None)
    app = create_app(ledger=Ledger(), paypal=PayPalClient(dry_run=True), service=service)
    full = app.openapi()

    paths = {p: op for p, op in full["paths"].items() if p.startswith(PUBLIC_PREFIXES)}
    spec: dict[str, Any] = {
        "openapi": full["openapi"],
        "info": {
            "title": "Trustee Platform API",
            "version": _project_version(),
            "description": INTRO + SIGNING_SCHEME,
        },
        "servers": SERVERS,
        "tags": TAGS,
        "paths": paths,
        "components": full.get("components", {}),
    }
    _prune_components(spec)
    return spec


def render(spec: dict[str, Any]) -> str:
    """Stable text: sorted keys and a trailing newline, so a regenerated file only diffs when the API changes."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render(build_spec())
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale; run scripts/export_openapi.py", file=sys.stderr)
            return 1
        print(f"{OUT} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main(sys.argv[1:]))
