"""Minimal OpenAI-compatible chat client.

Works with NIM (default), OpenAI, Ollama, or any OpenAI-compatible endpoint
via LLM_BASE_URL/LLM_API_KEY/LLM_MODEL. Keeps the dependency surface tiny and
the demo reproducible: temperature pinned low for deterministic-ish behavior.

Two knobs exist because hosted models change under us:
  * LLM_EXTRA_BODY — a JSON object merged into every request body. Reasoning
    models need it: NIM's Nemotron 3.5 spends its whole token budget "thinking"
    and returns empty content unless it gets
    `{"chat_template_kwargs": {"enable_thinking": false}}`.
  * ASTRO_GATEWAY_URL / ASTRO_GATEWAY_API_KEY / MODEL_DEFAULT — the Astropods AI Gateway, injected into a hosted
    agent. A fallback only: used when LLM_BASE_URL and LLM_API_KEY are both unset; LLM_MODEL beats MODEL_DEFAULT.
  * LLM_TIMEOUT — seconds per request (default 30). The OpenAI client's own
    default is ten minutes, which would freeze a demo run on one slow call; a
    timeout instead raises, and the runner falls back to the recorded moves.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_TIMEOUT = 30.0


def _extra_body_from_env() -> dict[str, Any] | None:
    raw = os.environ.get("LLM_EXTRA_BODY", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("LLM_EXTRA_BODY is not valid JSON; ignoring it")
        return None
    if not isinstance(value, dict):
        log.warning("LLM_EXTRA_BODY must be a JSON object; ignoring it")
        return None
    return value


def gateway_from_env() -> tuple[str, str, str | None] | None:
    """(base_url, api_key, model) of the Astropods AI Gateway, or None when it does not apply.

    It applies only when the container has both ASTRO_GATEWAY_URL and ASTRO_GATEWAY_API_KEY and the operator set
    neither LLM_BASE_URL nor LLM_API_KEY: an explicit LLM_* setting always wins, so a gateway key is never sent to
    another endpoint. The model is MODEL_DEFAULT, which LLM_MODEL overrides."""
    if os.environ.get("LLM_BASE_URL") or os.environ.get("LLM_API_KEY"):
        return None
    url = (os.environ.get("ASTRO_GATEWAY_URL") or "").strip().rstrip("/")
    key = os.environ.get("ASTRO_GATEWAY_API_KEY") or ""
    if not url or not key:
        return None
    return f"{url}/v1", key, os.environ.get("MODEL_DEFAULT") or None


class LLM:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        extra_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ):
        gateway = gateway_from_env()
        self.client = OpenAI(
            base_url=base_url
            or os.environ.get("LLM_BASE_URL")
            or (gateway[0] if gateway else "https://integrate.api.nvidia.com/v1"),
            api_key=api_key or os.environ.get("LLM_API_KEY") or (gateway[1] if gateway else "no-key"),
            timeout=timeout if timeout is not None else float(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT),
            max_retries=1,
        )
        self.model: str = (
            model
            if model is not None
            else (os.environ.get("LLM_MODEL") or (gateway[2] if gateway else None) or DEFAULT_MODEL)
        )
        self.temperature = temperature
        self.extra_body = extra_body if extra_body is not None else _extra_body_from_env()
        self.last_raw: str = ""

    def complete(self, system: str, messages: Sequence[dict[str, str]]) -> str:
        msgs: list[Any] = [{"role": "system", "content": system}, *messages]
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=msgs,
            extra_body=self.extra_body,
        )
        text = resp.choices[0].message.content or ""
        self.last_raw = text
        return text.strip()

    def complete_json(self, system: str, messages: Sequence[dict[str, str]]) -> dict[str, Any]:
        """LLM output forced through a JSON envelope; retry once on parse failure."""
        raw = ""
        for _ in range(2):
            raw = self.complete(system, messages)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.strip("`").removeprefix("json").strip()
            try:
                data: dict[str, Any] = json.loads(raw)
                return data
            except json.JSONDecodeError:
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."},
                ]
        raise ValueError(f"LLM did not return parseable JSON: {raw[:300]}")
