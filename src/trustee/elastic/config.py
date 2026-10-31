"""Elastic connection settings read from the environment, with the same https-only rule as the alert webhook."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_INDEX = "trustee-ledger"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ElasticConfigError(ValueError):
    """The Elastic configuration is unusable (for example a non-https URL)."""


def validate_url(url: str, what: str = "Elasticsearch") -> str:
    """https:// anywhere; http:// only for localhost / 127.0.0.1. Returns the URL without a trailing slash."""
    cleaned = url.strip()
    parts = urlsplit(cleaned)
    if parts.scheme == "https" and parts.hostname:
        return cleaned.rstrip("/")
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return cleaned.rstrip("/")
    raise ElasticConfigError(f"{what} URL must be https:// (http:// is only allowed for localhost/127.0.0.1)")


def url_from_cloud_id(cloud_id: str) -> str:
    """Elasticsearch endpoint encoded in an Elastic Cloud ID (`name:base64(host$es_id$kibana_id)`)."""
    encoded = cloud_id.split(":", 1)[-1]
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ElasticConfigError("ELASTIC_CLOUD_ID is not valid base64") from exc
    parts = decoded.split("$")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ElasticConfigError("ELASTIC_CLOUD_ID does not contain an Elasticsearch endpoint")
    host, _, port = parts[0].partition(":")
    return f"https://{parts[1]}.{host}" + (f":{port}" if port and port != "443" else "")


@dataclass(frozen=True)
class ElasticConfig:
    url: str
    api_key: str
    index: str = DEFAULT_INDEX

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"ApiKey {self.api_key}", "Content-Type": "application/json"}


def config_from_env(env: Mapping[str, str] | None = None) -> ElasticConfig | None:
    """Settings from ELASTICSEARCH_URL (or ELASTIC_CLOUD_ID), ELASTIC_API_KEY, ELASTIC_LEDGER_INDEX.

    None when Elastic is not configured at all. Raises ElasticConfigError when it is configured badly, so the
    caller can log it and carry on without Elastic.
    """
    env = os.environ if env is None else env
    url = (env.get("ELASTICSEARCH_URL") or "").strip()
    cloud_id = (env.get("ELASTIC_CLOUD_ID") or "").strip()
    if not url and not cloud_id:
        return None
    key = (env.get("ELASTIC_API_KEY") or "").strip()
    if not key:
        raise ElasticConfigError("ELASTIC_API_KEY is not set")
    index = (env.get("ELASTIC_LEDGER_INDEX") or "").strip() or DEFAULT_INDEX
    return ElasticConfig(url=validate_url(url or url_from_cloud_id(cloud_id)), api_key=key, index=index)


def arena_elastic_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("TRUSTEE_ELASTIC_ARENA") or "0").strip().lower() in ("1", "true", "yes", "on")
