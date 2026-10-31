"""Create the Elastic side of Trustee: ledger index template, Agent Builder ES|QL tools and the ledger analyst.

    KIBANA_URL=... ELASTICSEARCH_URL=... ELASTIC_API_KEY=... uv run python scripts/elastic_setup.py
    uv run python scripts/elastic_setup.py --dry-run          # print every request, send nothing

Idempotent: the index template is a PUT, and a tool or agent that already exists is updated in place.
Tool parameter types differ by version (`string` on Serverless and 9.4+, `keyword`/`long` on 9.2 to 9.3);
pass --legacy-param-types for 9.2 to 9.3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trustee.analyst import DEFAULT_AGENT_ID, card_url  # noqa: E402
from trustee.elastic import queries  # noqa: E402
from trustee.elastic.config import DEFAULT_INDEX, ElasticConfigError, validate_url  # noqa: E402
from trustee.elastic.mapping import index_template  # noqa: E402

TEMPLATE_NAME = "trustee-ledger"
SINCE_DEFAULT = "2020-01-01T00:00:00Z"
INSTRUCTIONS = (
    "You are the Trustee ledger analyst. Trustee is an escrow service for payments between AI agents; its ledger "
    "records every deal event. Answer only from the results of your tools, never from memory or guesses. Cite the "
    "deal id for every claim about a deal. If a tool returns no rows, say the ledger has nothing for that "
    "question. You can only read the ledger: you cannot move, refund, capture or void money, and you must say so "
    "if asked. Explain refunds and voids from the 'detail' column of the state and ruling rows. Keep answers short."
)


@dataclass
class Request:
    method: str
    url: str
    body: dict[str, Any] | None
    target: str  # "es" or "kibana"
    label: str
    update: tuple[str, dict[str, Any]] | None = None  # (PUT url, body) used when a POST finds the object exists


def _types(legacy: bool) -> dict[str, str]:
    return (
        {"string": "keyword", "integer": "integer", "date": "date"}
        if legacy
        else {"string": "string", "integer": "integer", "date": "date"}
    )


def tools(index: str, legacy: bool = False) -> list[dict[str, Any]]:
    t = _types(legacy)
    since = {
        "type": t["date"],
        "description": f"Only events at or after this ISO-8601 time. Defaults to {SINCE_DEFAULT}.",
        "optional": True,
        "defaultValue": SINCE_DEFAULT,
    }

    def tool(tool_id: str, description: str, query: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": tool_id,
            "type": "esql",
            "description": description,
            "tags": ["trustee"],
            "configuration": {"query": query, "params": params},
        }

    return [
        tool(
            "trustee-deal-history",
            "Every ledger event of one deal in time order: state changes, mandate checks, rulings, PayPal steps, "
            "with buyer, seller, amount and a 'detail' reason. Use it to explain what happened to a deal.",
            queries.deal_history_query(index),
            {"deal_id": {"type": t["string"], "description": "The deal id, for example deal_1a2b3c."}},
        ),
        tool(
            "trustee-spend",
            "Money an agent spent as buyer on deals that settled, by currency, since a date.",
            queries.spend_query(index),
            {"agent_id": {"type": t["string"], "description": "The buyer agent id."}, "since": since},
        ),
        tool(
            "trustee-earnings",
            "Money an agent received as seller on deals that settled, by currency, since a date.",
            queries.earnings_query(index),
            {"agent_id": {"type": t["string"], "description": "The seller agent id."}, "since": since},
        ),
        tool(
            "trustee-void-reasons",
            "Recent deals that were voided or refunded, newest first, with the buyer, seller, amount and the "
            "reason recorded when the escrow was released.",
            queries.void_reasons_query(index),
            {
                "since": since,
                "limit": {
                    "type": t["integer"],
                    "description": "Maximum rows. Defaults to 25.",
                    "optional": True,
                    "defaultValue": 25,
                },
            },
        ),
        tool(
            "trustee-agent-reputation",
            "One agent's track record as buyer or seller: deals, settled, voided, disputed, arbiter rulings "
            "lost as seller, median delivery seconds, first and last seen.",
            queries.reputation_query(index),
            {"agent": {"type": t["string"], "description": "The agent id."}},
        ),
    ]


def agent(tool_ids: list[str]) -> dict[str, Any]:
    return {
        "id": DEFAULT_AGENT_ID,
        "name": "Trustee Ledger Analyst",
        "description": "Answers questions about Trustee escrow deals from the ledger. Read-only.",
        "labels": ["trustee"],
        "configuration": {"instructions": INSTRUCTIONS, "tools": [{"tool_ids": tool_ids}]},
    }


def build_requests(es_url: str, kibana_url: str, index: str, legacy: bool = False) -> list[Request]:
    reqs = [
        Request("PUT", f"{es_url}/_index_template/{TEMPLATE_NAME}", index_template(index), "es", "index template"),
        Request("PUT", f"{es_url}/{index}", None, "es", f"index {index} (ok if it exists)"),
    ]
    base = f"{kibana_url}/api/agent_builder"
    defs = tools(index, legacy)
    for d in defs:
        update = {k: v for k, v in d.items() if k not in ("id", "type")}
        reqs.append(
            Request("POST", f"{base}/tools", d, "kibana", f"tool {d['id']}", (f"{base}/tools/{d['id']}", update))
        )
    body = agent([d["id"] for d in defs])
    update_agent = {k: v for k, v in body.items() if k != "id"}
    reqs.append(
        Request(
            "POST",
            f"{base}/agents",
            body,
            "kibana",
            f"agent {body['id']}",
            (f"{base}/agents/{body['id']}", update_agent),
        )
    )
    return reqs


def run(reqs: list[Request], api_key: str, http: httpx.Client) -> int:
    headers = {"Authorization": f"ApiKey {api_key}", "Content-Type": "application/json", "kbn-xsrf": "true"}
    failures = 0
    for r in reqs:
        resp = http.request(r.method, r.url, json=r.body, headers=headers)
        status = resp.status_code
        if status == 409 and r.update is not None:
            resp = http.put(r.update[0], json=r.update[1], headers=headers)
            status = resp.status_code
            note = "updated" if status < 300 else f"update failed (HTTP {status})"
        elif status == 400 and "resource_already_exists" in resp.text:
            note, status = "already exists", 200
        else:
            note = "ok" if status < 300 else f"failed (HTTP {status}): {resp.text[:200]}"
        print(f"{'ok  ' if status < 300 else 'FAIL'} {r.label}: {note}")
        failures += status >= 300
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the requests and send nothing")
    ap.add_argument("--legacy-param-types", action="store_true", help="tool parameter types for Elastic 9.2 to 9.3")
    ap.add_argument("--index", default=os.environ.get("ELASTIC_LEDGER_INDEX") or DEFAULT_INDEX)
    args = ap.parse_args(argv)
    try:
        es_url = validate_url(
            os.environ.get("ELASTICSEARCH_URL") or ("https://example.es.invalid" if args.dry_run else "")
        )
        kibana_url = validate_url(
            os.environ.get("KIBANA_URL") or ("https://example.kb.invalid" if args.dry_run else ""), "Kibana"
        )
    except ElasticConfigError as exc:
        print(f"error: {exc} (set ELASTICSEARCH_URL and KIBANA_URL)", file=sys.stderr)
        return 2
    reqs = build_requests(es_url, kibana_url, args.index, args.legacy_param_types)
    if args.dry_run:
        for r in reqs:
            print(f"# {r.label}\n{r.method} {r.url}")
            if r.body is not None:
                print(json.dumps(r.body, indent=2))
        print(f"\nA2A agent card (after a real run): {card_url(kibana_url)}")
        return 0
    key = (os.environ.get("ELASTIC_API_KEY") or "").strip()
    if not key:
        print("error: ELASTIC_API_KEY is not set", file=sys.stderr)
        return 2
    with httpx.Client(timeout=30.0) as http:
        failures = run(reqs, key, http)
    if failures:
        print(f"{failures} request(s) failed", file=sys.stderr)
        return 1
    print(f"\nA2A agent card: {card_url(kibana_url)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
