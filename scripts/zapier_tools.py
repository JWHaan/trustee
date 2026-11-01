"""List the tools on a Zapier MCP connection, or preview what an agent would book for a sample deal.

    uv run python scripts/zapier_tools.py --agent tidewater            # names of the tools on its connection
    uv run python scripts/zapier_tools.py --agent tidewater --dry-run  # the calls it would make, no network

The connection token is read from <AGENT>_ZAPIER_MCP_TOKEN and is never printed. Tool names are printed; no tool
output is. Use the names to fill <AGENT>_ZAPIER_TOOLS, for example
TIDEWATER_ZAPIER_TOOLS='{"record_invoice": "quickbooks_create_invoice"}'.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from trustee.network.backoffice import (
    ACTIONS_BY_AGENT,
    DEFAULT_URL,
    BackofficeConfigError,
    ZapierMcpSession,
    actions_for_receipt,
    build_arguments,
    facts_from_receipt,
    is_money_tool,
    parse_tool_map,
    validate_url,
)

SAMPLE_RECEIPT: dict[str, Any] = {
    "type": "trustee.settlement_receipt",
    "version": 1,
    "deal_id": "deal_sample",
    "outcome": "settled",
    "amount": "42.00",
    "currency": "USD",
    "buyer": "northwind-planner",
    "seller": "tidewater-sales",
    "paypal": {"capture_id": "CAPTURE-SAMPLE", "order_id": "ORDER-SAMPLE"},
    "signature": "not-checked-by-this-script",
}


def dry_run(agent: str, tools: dict[str, str]) -> int:
    counterparty = "Northwind Outfitters" if agent == "tidewater" else "Tidewater Data Co."
    facts = facts_from_receipt(SAMPLE_RECEIPT, counterparty)
    for action in actions_for_receipt(agent, facts.outcome):
        tool = tools.get(action)
        if tool is None:
            print(f"{action}: not mapped, would be skipped")
            continue
        print(f"{action}: would call {tool} with {json.dumps(build_arguments(action, facts), sort_keys=True)}")
    return 0


def list_tools(agent: str, tools: dict[str, str], token: str) -> int:
    url = validate_url(os.environ.get("ZAPIER_MCP_URL") or DEFAULT_URL)
    try:
        session = ZapierMcpSession(url, token)
    except ImportError:
        print("the zapier extra is not installed: uv sync --extra zapier", file=sys.stderr)
        return 1
    try:
        names = session.list_tools()
    except Exception as exc:  # noqa: BLE001 - print the error kind only; the transport text can echo headers
        print(f"could not list tools: {type(exc).__name__} (check the token and the server URL)", file=sys.stderr)
        return 1
    for name in sorted(names):
        notes = []
        mapped = [a for a, t in tools.items() if t == name]
        if mapped:
            notes.append(f"mapped to {mapped[0]}")
        if is_money_tool(name):
            notes.append("refused: money-moving name")
        print(f"{name}" + (f"   ({'; '.join(notes)})" if notes else ""))
    print(f"{len(names)} tools on the {agent} connection", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Zapier MCP tools for a Trustee network agent")
    ap.add_argument("--agent", choices=sorted(ACTIONS_BY_AGENT), required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the calls for a sample receipt; no network")
    args = ap.parse_args(argv)
    prefix = args.agent.upper()
    try:
        tools = parse_tool_map(os.environ.get(f"{prefix}_ZAPIER_TOOLS"), args.agent)
    except BackofficeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        return dry_run(args.agent, tools)
    token = (os.environ.get(f"{prefix}_ZAPIER_MCP_TOKEN") or "").strip()
    if not token:
        print(f"set {prefix}_ZAPIER_MCP_TOKEN first (Zapier MCP server, Settings tab, API token)", file=sys.stderr)
        return 1
    return list_tools(args.agent, tools, token)


if __name__ == "__main__":
    raise SystemExit(main())
