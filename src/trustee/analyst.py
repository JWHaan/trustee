"""Ask the Elastic Agent Builder ledger analyst a question over A2A.

The analyst is an Agent Builder agent (`trustee-ledger-analyst`, created by scripts/elastic_setup.py) that
answers from ES|QL tools over the shipped ledger. It can read the ledger and nothing else: it has no tool that
moves money, and Trustee never calls it from the payment path.

Agent Builder serves each agent over A2A (JSON-RPC 0.3 method names, no streaming):
  GET  {KIBANA_URL}/api/agent_builder/a2a/{agent_id}.json   (agent card)
  POST {KIBANA_URL}/api/agent_builder/a2a/{agent_id}        (message/send)
Both need `Authorization: ApiKey <key>`.

    KIBANA_URL=... ELASTIC_API_KEY=... python -m trustee.analyst "why was deal X refunded?"
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

from .elastic.config import ElasticConfigError, validate_url

DEFAULT_AGENT_ID = "trustee-ledger-analyst"


class AnalystError(RuntimeError):
    """The analyst could not be reached or answered with an error."""


def card_url(kibana_url: str, agent_id: str = DEFAULT_AGENT_ID) -> str:
    return f"{kibana_url.rstrip('/')}/api/agent_builder/a2a/{agent_id}.json"


def rpc_url(kibana_url: str, agent_id: str = DEFAULT_AGENT_ID) -> str:
    return f"{kibana_url.rstrip('/')}/api/agent_builder/a2a/{agent_id}"


def _texts(parts: Any) -> list[str]:
    return [
        str(p["text"]) for p in parts or [] if isinstance(p, dict) and p.get("kind", "text") == "text" and p.get("text")
    ]


def answer_text(result: dict[str, Any]) -> str:
    """The text of a `message/send` result, which is either a Message or a Task."""
    if result.get("kind") == "message" or "parts" in result:
        return "\n".join(_texts(result.get("parts")))
    chunks: list[str] = []
    for artifact in result.get("artifacts") or []:
        chunks.extend(_texts(artifact.get("parts")))
    if chunks:
        return "".join(chunks)
    status_message = (result.get("status") or {}).get("message") or {}
    return "\n".join(_texts(status_message.get("parts")))


class LedgerAnalyst:
    def __init__(
        self,
        kibana_url: str,
        api_key: str,
        agent_id: str = DEFAULT_AGENT_ID,
        http: httpx.Client | None = None,
        timeout: float = 120.0,
    ):
        self.kibana_url = validate_url(kibana_url, "Kibana")
        self.agent_id = agent_id
        self._api_key = api_key
        self._http = http if http is not None else httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"ApiKey {self._api_key}", "Content-Type": "application/json"}

    def card(self) -> dict[str, Any]:
        resp = self._http.get(card_url(self.kibana_url, self.agent_id), headers=self._headers())
        if resp.status_code != 200:
            raise AnalystError(f"agent card request answered HTTP {resp.status_code}")
        card: dict[str, Any] = resp.json()
        return card

    def ask(self, question: str) -> str:
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": question}],
                },
                "configuration": {"blocking": True},
            },
        }
        try:
            resp = self._http.post(rpc_url(self.kibana_url, self.agent_id), json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise AnalystError(f"could not reach Kibana: {type(exc).__name__}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 300 or payload.get("error"):
            detail = (payload.get("error") or {}).get("message") or payload.get("message") or ""
            raise AnalystError(f"the analyst answered HTTP {resp.status_code}: {str(detail)[:300]}")
        text = answer_text(payload.get("result") or {})
        if not text:
            raise AnalystError("the analyst returned no text")
        return text

    def close(self) -> None:
        self._http.close()


def analyst_from_env(env: Mapping[str, str] | None = None, http: httpx.Client | None = None) -> LedgerAnalyst:
    env = os.environ if env is None else env
    kibana = (env.get("KIBANA_URL") or "").strip()
    key = (env.get("ELASTIC_API_KEY") or "").strip()
    if not kibana or not key:
        raise AnalystError("KIBANA_URL and ELASTIC_API_KEY must be set")
    try:
        return LedgerAnalyst(kibana, key, env.get("ELASTIC_ANALYST_AGENT_ID") or DEFAULT_AGENT_ID, http=http)
    except ElasticConfigError as exc:
        raise AnalystError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trustee.analyst", description="Ask the Elastic ledger analyst")
    ap.add_argument("question", nargs="+", help="for example: why was deal deal_123 refunded?")
    args = ap.parse_args(argv)
    try:
        analyst = analyst_from_env()
        try:
            print(analyst.ask(" ".join(args.question)))
        finally:
            analyst.close()
    except AnalystError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
