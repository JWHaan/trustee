"""Minimal OpenAI-compatible chat client.

Works with NIM (default), OpenAI, Ollama, or any OpenAI-compatible endpoint
via LLM_BASE_URL/LLM_API_KEY/LLM_MODEL. Keeps the dependency surface tiny and
the demo reproducible: temperature pinned low for deterministic-ish behavior.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

from openai import OpenAI


class LLM:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
    ):
        self.client = OpenAI(
            base_url=base_url or os.environ.get("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            api_key=api_key or os.environ.get("LLM_API_KEY", "no-key"),
        )
        self.model = model or os.environ.get("LLM_MODEL", "meta/llama-3.1-70b-instruct")
        self.temperature = temperature
        self.last_raw: str = ""

    def complete(self, system: str, messages: Sequence[dict[str, str]]) -> str:
        msgs: list[Any] = [{"role": "system", "content": system}, *messages]
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=msgs,
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
                return json.loads(raw)
            except json.JSONDecodeError:
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."},
                ]
        raise ValueError(f"LLM did not return parseable JSON: {raw[:300]}")
