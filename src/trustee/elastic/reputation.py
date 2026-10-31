"""Reputation from the shipped ledger index: one ES|QL query, same output as `LocalReputation`."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from ..reputation import Reputation, now_iso
from .config import ElasticConfig
from .queries import reputation_query

log = logging.getLogger(__name__)


class ElasticReputationError(RuntimeError):
    """Elasticsearch could not answer (network, auth, or a malformed response)."""


def rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    """An ES|QL `{columns, values}` response as a list of dicts."""
    names = [str(c["name"]) for c in body.get("columns", [])]
    return [dict(zip(names, row, strict=False)) for row in body.get("values", [])]


def parse_reputation(agent_id: str, body: dict[str, Any]) -> Reputation:
    """Turn the single-row reputation response into a `Reputation`. No row or all-null means no history."""
    found = rows(body)
    row = found[0] if found else {}

    def count(name: str) -> int:
        value = row.get(name)
        return int(value) if value is not None else 0

    total = count("deals_total")
    disputed = count("disputed")
    median = row.get("median_delivery_s")
    return Reputation(
        agent_id=agent_id,
        deals_total=total,
        settled=count("settled"),
        voided=count("voided"),
        disputed=disputed,
        rulings_lost=count("rulings_lost"),
        dispute_rate=round(disputed / total, 4) if total else 0.0,
        median_delivery_seconds=round(float(median), 3) if median is not None else None,
        first_seen=row.get("first_seen") or None,
        last_seen=row.get("last_seen") or None,
        source="elastic",
        as_of=now_iso(),
    )


class ElasticReputation:
    def __init__(
        self,
        config: ElasticConfig,
        http: httpx.Client | None = None,
        timeout: float = 10.0,
        cache_seconds: float = 30.0,
    ):
        self.config = config
        self._http = http if http is not None else httpx.Client(timeout=timeout)
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str | None], tuple[float, Reputation]] = {}
        self._lock = threading.Lock()

    def reputation(self, agent_id: str, exclude_deal: str | None = None) -> Reputation:
        key = (agent_id, exclude_deal)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self._cache_seconds:
            return hit[1]
        params: list[dict[str, Any]] = [{"agent": agent_id}]
        if exclude_deal:
            params.append({"exclude_deal": exclude_deal})
        try:
            resp = self._http.post(
                f"{self.config.url}/_query",
                json={"query": reputation_query(self.config.index, bool(exclude_deal)), "params": params},
                headers=self.config.headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ElasticReputationError(f"Elasticsearch unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise ElasticReputationError(f"Elasticsearch answered HTTP {resp.status_code}")
        try:
            result = parse_reputation(agent_id, resp.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise ElasticReputationError("Elasticsearch returned an unreadable ES|QL response") from exc
        with self._lock:
            self._cache[key] = (now, result)
            if len(self._cache) > 1000:
                self._cache.pop(next(iter(self._cache)))
        return result


def reputation_from_env(env: Any = None) -> ElasticReputation | None:
    """The Elastic backend when TRUSTEE_REPUTATION_BACKEND=elastic and Elastic is configured; else None
    (the caller keeps the local backend)."""
    import os

    from .config import ElasticConfigError, config_from_env

    env = os.environ if env is None else env
    if (env.get("TRUSTEE_REPUTATION_BACKEND") or "local").strip().lower() != "elastic":
        return None
    try:
        config = config_from_env(env)
    except ElasticConfigError as exc:
        log.error("elastic reputation disabled, using the local backend: %s", exc)
        return None
    if config is None:
        log.error("TRUSTEE_REPUTATION_BACKEND=elastic but ELASTICSEARCH_URL is not set; using the local backend")
        return None
    return ElasticReputation(config)
